using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class ExtractQdbVariableType
{
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi)]
    private static extern IntPtr LoadLibrary(string name);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    [DllImport("kernel32.dll")]
    private static extern uint SetErrorMode(uint mode);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr OpenDb(IntPtr path);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr GetSpec(uint type);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr GetItem(
        IntPtr db,
        uint type,
        uint mode,
        uint key,
        uint argument5,
        uint size,
        IntPtr buffer,
        IntPtr output
    );

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int ControlSyncStatus(IntPtr db);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void ControlSync(IntPtr db, int enabled);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int CloseDb(IntPtr db);

    private static T Load<T>(IntPtr module, string name) where T : class
    {
        var address = GetProcAddress(module, name);
        if (address == IntPtr.Zero)
            throw new InvalidOperationException("missing export " + name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
    }

    private static byte[] ReadItem(
        GetItem get,
        IntPtr db,
        uint type,
        uint key,
        int baseSize,
        IntPtr output
    )
    {
        var buffer = Marshal.AllocHGlobal(baseSize);
        try
        {
            for (var offset = 0; offset < baseSize; offset++)
                Marshal.WriteByte(buffer, offset, 0);
            Marshal.WriteInt32(output, 0, 0);
            var item = get(db, type, 0, key, 0, (uint)baseSize, buffer, output);
            if (item == IntPtr.Zero)
                return null;
            var actualSize = Marshal.ReadInt32(output, 0);
            if (actualSize < baseSize || actualSize > 0x10000000)
                throw new InvalidOperationException(
                    string.Format("invalid size {0} for key {1}", actualSize, key)
                );
            if (actualSize == baseSize)
            {
                var row = new byte[baseSize];
                Marshal.Copy(buffer, row, 0, baseSize);
                return row;
            }

            var fullBuffer = Marshal.AllocHGlobal(actualSize);
            try
            {
                for (var offset = 0; offset < actualSize; offset++)
                    Marshal.WriteByte(fullBuffer, offset, 0);
                Marshal.WriteInt32(output, 0, 0);
                item = get(db, type, 0, key, 0, (uint)actualSize, fullBuffer, output);
                if (item == IntPtr.Zero || Marshal.ReadInt32(output, 0) != actualSize)
                    throw new InvalidOperationException(
                        string.Format("full read failed for key {0}", key)
                    );
                var row = new byte[actualSize];
                Marshal.Copy(fullBuffer, row, 0, actualSize);
                return row;
            }
            finally
            {
                Marshal.FreeHGlobal(fullBuffer);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static int Main(string[] args)
    {
        if (args.Length < 3 || args.Length > 4 ||
            (args.Length == 4 && args[3] != QdbPassword.PasswordStdinArgument))
            throw new ArgumentException(
                "usage: Extract-QdbVariableType.exe input.qdf type-hex output.bin [--password-stdin]"
            );

        SetErrorMode(0x8003);
        var type = Convert.ToUInt32(args[1], 16);
        foreach (var library in new[] { "QWUTIL.dll", "QACCESS.dll", "QDAPP.dll", "CASHFLOW.dll" })
            if (LoadLibrary(library) == IntPtr.Zero)
                throw new InvalidOperationException("LoadLibrary " + library + " failed");
        var module = LoadLibrary("QDB.dll");
        if (module == IntPtr.Zero)
            throw new InvalidOperationException("LoadLibrary QDB.dll failed");

        var openNoPassword = Load<OpenDb>(module, "_QDBNet_OpenDBNoPassword@4");
        var open = Load<OpenDb>(module, "QDBOpenDB");

        var spec = Load<GetSpec>(module, "QDBGetItemSpec")(type);
        var baseSize = spec == IntPtr.Zero ? 0 : Marshal.ReadInt32(spec, 0x20);
        if (baseSize <= 0 || baseSize > 0x100000)
            throw new InvalidOperationException("invalid base size " + baseSize);

        var datafilePassword = QdbPassword.ReadOptionalPassword(args, 3);
        var encodedPath = Encoding.Default.GetBytes(args[0] + "\0");
        var path = Marshal.AllocHGlobal(encodedPath.Length);
        Marshal.Copy(encodedPath, 0, path, encodedPath.Length);
        IntPtr db;
        try
        {
            db = QdbPassword.OpenOrThrow(
                module,
                path,
                datafilePassword,
                p => openNoPassword(p),
                p => open(p)
            );
        }
        catch (QdbPassword.PasswordRequiredException error)
        {
            Console.Error.WriteLine(error.Message);
            Marshal.FreeHGlobal(path);
            return 4;
        }
        catch (InvalidOperationException error)
        {
            Console.Error.WriteLine(error.Message);
            Marshal.FreeHGlobal(path);
            return 3;
        }

        try
        {
            var get = Load<GetItem>(module, "QDB2GetItem");
            var controlSync = Load<ControlSync>(module, "QDBControlSync");
            var priorSyncStatus = Load<ControlSyncStatus>(module, "_QDBControlSyncStatus@4")(db);
            controlSync(db, 0);
            var output = Marshal.AllocHGlobal(4);
            try
            {
                var count = 0;
                using (var stream = File.Create(args[2]))
                using (var writer = new BinaryWriter(stream))
                {
                    writer.Write(new byte[] { (byte)'Q', (byte)'V', (byte)'A', (byte)'R' });
                    writer.Write(1);
                    writer.Write(baseSize);
                    writer.Write(0);
                    for (uint key = 1; key <= 1000000; key++)
                    {
                        var row = ReadItem(get, db, type, key, baseSize, output);
                        if (row == null)
                            break;
                        writer.Write(key);
                        writer.Write(row.Length);
                        writer.Write(row);
                        count++;
                    }
                    stream.Position = 12;
                    writer.Write(count);
                }
                Console.WriteLine(
                    string.Format(
                        "type=0x{0:x} count={1} base-size={2} output={3}",
                        type,
                        count,
                        baseSize,
                        args[2]
                    )
                );
            }
            finally
            {
                Marshal.FreeHGlobal(output);
                controlSync(db, priorSyncStatus);
            }
        }
        finally
        {
            Load<CloseDb>(module, "QDBCloseDB")(db);
            Marshal.FreeHGlobal(path);
        }

        return 0;
    }
}
