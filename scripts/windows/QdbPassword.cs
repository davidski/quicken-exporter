using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// The password bridge is deliberately isolated from the record extractors.
// qdb.dll exposes the final primitive-password decrypt entry point, but not
// the small password-to-primitive builder used by the matching Quicken build.
// The builder is located by an exact byte signature (unique in 27.1.68.31 at
// RVA 0x2eb30 and 27.1.69.53 at 0x2ec55) on builds at or above a minimum version.
internal static class QdbPassword
{
    public sealed class PasswordRequiredException : InvalidOperationException
    {
        public PasswordRequiredException(string message) : base(message) { }
    }

    public const string PasswordStdinArgument = "--password-stdin";
    private static readonly Version MinimumQdbVersion = new Version(27, 1, 68, 31);

    // Whole builder body through `ret 8`; its rel32 calls are stable across builds so far.
    private static readonly byte[] PasswordPrimitiveBuilderSignature =
    {
        0x55, 0x8b, 0xec, 0x83, 0xec, 0x48, 0x56, 0x57, 0x33, 0xc0, 0x8d, 0x7d, 0xf4, 0xab, 0xab, 0x6a,
        0x01, 0x66, 0xab, 0xe8, 0xa6, 0xfe, 0xff, 0xff, 0x8d, 0x4d, 0xb8, 0x51, 0x8d, 0x4d, 0xf4, 0x51,
        0x50, 0xff, 0x75, 0x08, 0xe8, 0xdc, 0xfc, 0xff, 0xff, 0x8b, 0x7d, 0x0c, 0x83, 0xc4, 0x14, 0x6a,
        0x05, 0x59, 0x8d, 0x75, 0xc6, 0xf3, 0xa5, 0x33, 0xc0, 0x5f, 0x40, 0x5e, 0xc9, 0xc2, 0x08, 0x00,
    };

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int BuildPrimitivePassword(IntPtr password, IntPtr output);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int DecryptFileWithPrimitivePassword(IntPtr path, IntPtr primitive);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetModuleFileName(
        IntPtr module,
        StringBuilder filename,
        int size
    );

    private static T Load<T>(IntPtr module, string name) where T : class
    {
        var address = GetProcAddress(module, name);
        if (address == IntPtr.Zero) throw new InvalidOperationException("missing export " + name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(address, typeof(T));
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    public static string ReadOptionalPassword(string[] args, int requiredArgumentCount)
    {
        if (args.Length == requiredArgumentCount) return null;
        if (args.Length != requiredArgumentCount + 1 || args[requiredArgumentCount] != PasswordStdinArgument)
            throw new ArgumentException(
                "the optional password must be supplied through --password-stdin"
            );

        using (var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), false))
        {
            var encoded = reader.ReadLine();
            if (encoded == null)
                throw new InvalidOperationException("password input was not supplied on standard input");

            byte[] passwordBytes;
            try
            {
                passwordBytes = Convert.FromBase64String(encoded);
            }
            catch (FormatException)
            {
                throw new InvalidOperationException("password input was not valid base64");
            }

            try
            {
                return Encoding.UTF8.GetString(passwordBytes);
            }
            finally
            {
                Array.Clear(passwordBytes, 0, passwordBytes.Length);
            }
        }
    }

    public static IntPtr OpenOrThrow(
        IntPtr module,
        IntPtr path,
        string datafilePassword,
        Func<IntPtr, IntPtr> openNoPassword,
        Func<IntPtr, IntPtr> open
    )
    {
        var db = openNoPassword(path);
        if (db == IntPtr.Zero) db = open(path);
        if (db != IntPtr.Zero) return db;

        if (datafilePassword == null)
        {
            throw new PasswordRequiredException(
                "QDF appears to require a data-file password. "
                    + "Supply -DatafilePassword as a SecureString, or use -PromptForPassword."
            );
        }

        EnsureSupportedBuild(module);
        var builder = FindPasswordPrimitiveBuilder(module);
        if (!TryUnlock(module, builder, path, datafilePassword))
        {
            throw new InvalidOperationException(
                "The supplied data-file password was rejected by qdb.dll."
            );
        }

        db = open(path);
        if (db == IntPtr.Zero)
        {
            throw new InvalidOperationException(
                "qdb.dll accepted the password but could not open the QDF."
            );
        }
        return db;
    }

    private static bool TryUnlock(IntPtr module, IntPtr builder, IntPtr path, string datafilePassword)
    {
        var build = (BuildPrimitivePassword)Marshal.GetDelegateForFunctionPointer(
            builder,
            typeof(BuildPrimitivePassword)
        );
        var decrypt = Load<DecryptFileWithPrimitivePassword>(
            module,
            "_DecryptFileWithPrimitivePassword@8"
        );
        var passwordBytes = Encoding.Default.GetBytes(datafilePassword + "\0");
        var password = Marshal.AllocHGlobal(passwordBytes.Length);
        var primitive = Marshal.AllocHGlobal(20);
        try
        {
            Marshal.Copy(passwordBytes, 0, password, passwordBytes.Length);
            if (build(password, primitive) == 0) return false;
            return decrypt(path, primitive) != 0;
        }
        finally
        {
            ZeroMemory(primitive, 20);
            ZeroMemory(password, passwordBytes.Length);
            Array.Clear(passwordBytes, 0, passwordBytes.Length);
            Marshal.FreeHGlobal(primitive);
            Marshal.FreeHGlobal(password);
        }
    }

    private static void ZeroMemory(IntPtr address, int length)
    {
        for (var i = 0; i < length; i++) Marshal.WriteByte(address, i, 0);
    }

    private static void EnsureSupportedBuild(IntPtr module)
    {
        var filename = new StringBuilder(260);
        if (GetModuleFileName(module, filename, filename.Capacity) == 0)
        {
            throw new InvalidOperationException(
                "Cannot identify qdb.dll; password unlock is disabled for an unknown build."
            );
        }

        var info = FileVersionInfo.GetVersionInfo(filename.ToString());
        var version = new Version(info.FileMajorPart, info.FileMinorPart, info.FileBuildPart, info.FilePrivatePart);
        if (version < MinimumQdbVersion)
        {
            throw new InvalidOperationException(
                "Password unlock requires qdb.dll version "
                    + MinimumQdbVersion
                    + " or newer; found "
                    + version
                    + "."
            );
        }
    }

    private static IntPtr FindPasswordPrimitiveBuilder(IntPtr module)
    {
        // SizeOfImage lives at e_lfanew + 0x50 in the PE optional header.
        var imageSize = Marshal.ReadInt32(module, 0x50 + Marshal.ReadInt32(module, 0x3c));
        var image = new byte[imageSize];
        Marshal.Copy(module, image, 0, imageSize);

        var sig = PasswordPrimitiveBuilderSignature;
        var found = -1;
        for (var i = 0; i <= image.Length - sig.Length; i++)
        {
            var j = 0;
            while (j < sig.Length && image[i + j] == sig[j]) j++;
            if (j < sig.Length) continue;
            if (found >= 0)
                throw new InvalidOperationException(
                    "Password builder signature is ambiguous in this qdb.dll; password unlock is disabled."
                );
            found = i;
        }
        if (found < 0)
            throw new InvalidOperationException(
                "Password builder not found in this qdb.dll build; password unlock is not supported yet."
            );
        return IntPtr.Add(module, found);
    }
}
