using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class ExtractQdbReports
{
    private const uint ReportType = 0x120;

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr LoadLibrary(string name);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    [DllImport("kernel32.dll")]
    private static extern uint SetErrorMode(uint mode);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr OpenDb(IntPtr path);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int NumItems(IntPtr db, uint type);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr GetSpec(uint type);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr GetItem2(
        IntPtr db,
        uint type,
        uint mode,
        uint key,
        uint arg5,
        uint size,
        IntPtr buffer,
        IntPtr output
    );

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int CloseDb(IntPtr db);

    private static T Load<T>(IntPtr module, string name) where T : class
    {
        var address = GetProcAddress(module, name);
        if (address == IntPtr.Zero) throw new InvalidOperationException("missing export " + name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
    }

    public static int Main(string[] args)
    {
        if (args.Length < 2 || args.Length > 3 ||
            (args.Length == 3 && args[2] != QdbPassword.PasswordStdinArgument))
            throw new ArgumentException(
                "usage: Extract-QdbReports.exe input.qdf output-dir [--password-stdin]"
            );

        SetErrorMode(0x8003);
        Directory.CreateDirectory(args[1]);
        if (LoadLibrary("QWUTIL.dll") == IntPtr.Zero)
            throw new InvalidOperationException("LoadLibrary QWUTIL.dll failed");
        var module = LoadLibrary("QDB.dll");
        if (module == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary QDB.dll failed");

        var openNoPassword = Load<OpenDb>(module, "_QDBNet_OpenDBNoPassword@4");
        var open = Load<OpenDb>(module, "QDBOpenDB");
        var close = Load<CloseDb>(module, "QDBCloseDB");
        var numItems = Load<NumItems>(module, "QDBNumItems");
        var getSpec = Load<GetSpec>(module, "QDBGetItemSpec");
        var getItem = Load<GetItem2>(module, "QDB2GetItem");

        var pathBytes = Encoding.Default.GetBytes(args[0] + "\0");
        var path = Marshal.AllocHGlobal(pathBytes.Length);
        Marshal.Copy(pathBytes, 0, path, pathBytes.Length);
        try
        {
            var datafilePassword = QdbPassword.ReadOptionalPassword(args, 2);
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
                return 4;
            }
            catch (InvalidOperationException error)
            {
                Console.Error.WriteLine(error.Message);
                return 3;
            }
            try
            {
                ExtractReports(db, args[1], numItems, getSpec, getItem);
            }
            finally
            {
                close(db);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(path);
        }
        return 0;
    }

    private static void ExtractReports(
        IntPtr db,
        string outputDirectory,
        NumItems numItems,
        GetSpec getSpec,
        GetItem2 getItem
    )
    {
        var count = numItems(db, ReportType);
        var spec = getSpec(ReportType);
        var size = spec == IntPtr.Zero ? 0 : Marshal.ReadInt32(spec, 0x20);
        if (count < 0 || size <= 0 || size > 0x100000)
            throw new InvalidOperationException(
                string.Format("invalid report family: count={0} size={1}", count, size)
            );

        var records = new List<byte[]>();
        var fullRecords = new List<byte[]>();
        var actualSizes = new List<int>();
        var itemBuffer = Marshal.AllocHGlobal(size);
        var output = Marshal.AllocHGlobal(4);
        try
        {
            for (uint key = 1; key <= (uint)count; key++)
            {
                for (var index = 0; index < size; index++) Marshal.WriteByte(itemBuffer, index, 0);
                Marshal.WriteInt32(output, 0, 0);
                var item = getItem(db, ReportType, 0, key, 0, (uint)size, itemBuffer, output);
                if (item == IntPtr.Zero)
                    throw new InvalidOperationException(
                        string.Format("QDB2GetItem failed for report key {0}", key)
                    );
                actualSizes.Add(Marshal.ReadInt32(output, 0));
                var record = new byte[size];
                Marshal.Copy(itemBuffer, record, 0, size);
                records.Add(record);

                var actualSize = actualSizes[actualSizes.Count - 1];
                if (actualSize < size || actualSize > 0x1000000)
                    throw new InvalidOperationException(
                        string.Format("invalid report size for key {0}: {1}", key, actualSize)
                    );
                if (actualSize == size)
                {
                    fullRecords.Add(record);
                    continue;
                }

                var fullBuffer = Marshal.AllocHGlobal(actualSize);
                try
                {
                    for (var index = 0; index < actualSize; index++)
                        Marshal.WriteByte(fullBuffer, index, 0);
                    Marshal.WriteInt32(output, 0, 0);
                    item = getItem(
                        db,
                        ReportType,
                        0,
                        key,
                        0,
                        (uint)actualSize,
                        fullBuffer,
                        output
                    );
                    if (item == IntPtr.Zero)
                        throw new InvalidOperationException(
                            string.Format("QDB2GetItem full read failed for report key {0}", key)
                        );
                    if (Marshal.ReadInt32(output, 0) != actualSize)
                        throw new InvalidOperationException(
                            string.Format("report size changed while reading key {0}", key)
                        );
                    var fullRecord = new byte[actualSize];
                    Marshal.Copy(fullBuffer, fullRecord, 0, actualSize);
                    fullRecords.Add(fullRecord);
                }
                finally
                {
                    Marshal.FreeHGlobal(fullBuffer);
                }
            }
        }

        finally
        {
            Marshal.FreeHGlobal(output);
            Marshal.FreeHGlobal(itemBuffer);
        }

        using (
            var writer = new StreamWriter(
                Path.Combine(outputDirectory, "qdb-report-records.tsv"),
                false,
                new UTF8Encoding(false)
            )
        )
        {
            writer.WriteLine("key\tbase_size\tactual_size");
            for (var index = 0; index < records.Count; index++)
                writer.WriteLine(
                    string.Format("{0}\t{1}\t{2}", index + 1, size, actualSizes[index])
                );
        }

        var recordPath = Path.Combine(outputDirectory, "qdb-type-120.bin");
        using (var stream = new BinaryWriter(File.Create(recordPath)))
        {
            stream.Write(size);
            stream.Write(count);
            foreach (var record in records) stream.Write(record);
        }

        var fullRecordPath = Path.Combine(outputDirectory, "qdb-reports.bin");
        using (var stream = new BinaryWriter(File.Create(fullRecordPath)))
        {
            stream.Write(new byte[] { (byte)'Q', (byte)'R', (byte)'P', (byte)'T' });
            stream.Write(1);
            stream.Write(size);
            stream.Write(count);
            for (var index = 0; index < fullRecords.Count; index++)
            {
                stream.Write(index + 1);
                stream.Write(fullRecords[index].Length);
                stream.Write(fullRecords[index]);
            }
        }

        Console.WriteLine(
            string.Format(
                "type=0x{0:x} count={1} base-size={2}",
                ReportType,
                count,
                size
            )
        );
    }
}
