using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// The password bridge is deliberately isolated from the record extractors.
// qdb.dll exposes the final primitive-password decrypt entry point, but not
// the small password-to-primitive builder used by the matching Quicken build.
// The builder RVA is therefore guarded by the exact qdb.dll file version.
internal static class QdbPassword
{
    public sealed class PasswordRequiredException : InvalidOperationException
    {
        public PasswordRequiredException(string message) : base(message) { }
    }

    public const string PasswordStdinArgument = "--password-stdin";
    private const string SupportedQdbVersion = "27.1.68.31";
    private const int PasswordPrimitiveBuilderRva = 0x2eb30;

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
        if (!TryUnlock(module, path, datafilePassword))
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

    private static bool TryUnlock(IntPtr module, IntPtr path, string datafilePassword)
    {
        var build = (BuildPrimitivePassword)Marshal.GetDelegateForFunctionPointer(
            IntPtr.Add(module, PasswordPrimitiveBuilderRva),
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

        var version = FileVersionInfo.GetVersionInfo(filename.ToString()).FileVersion;
        if (!string.Equals(version, SupportedQdbVersion, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "Password unlock requires qdb.dll version "
                    + SupportedQdbVersion
                    + "; found "
                    + (version ?? "unknown")
                    + "."
            );
        }
    }
}
