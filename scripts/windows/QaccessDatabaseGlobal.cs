using System;
using System.Globalization;
using System.Runtime.InteropServices;

// qaccess.dll keeps the current-database QDB handle in a module global that
// its account and transaction APIs read instead of taking a handle argument.
// The global is not exported and its RVA moves between Quicken builds
// (0x2b9efc in 27.1.68.31, 0x2bd3dc in 27.1.69.26). A fixed offset therefore
// overwrites an unrelated variable on any other build, and the qaccess-backed
// sidecars (memos, splits, clear status, investment transactions) come back
// empty without any error.
//
// ACCT_BuildAcctList(flags, db) falls back to that same global when db is 0:
//
//     8B 45 0C          mov  eax, [ebp+0Ch]
//     85 C0             test eax, eax
//     75 05             jnz  +5
//     A1 xx xx xx xx    mov  eax, [global]
//
// so the address is read from the export's own (already relocated) code.
// QDF_QACCESS_DB_GLOBAL_RVA overrides the search for a build where that
// sequence differs.
internal static class QaccessDatabaseGlobal
{
    public const string OverrideVariable = "QDF_QACCESS_DB_GLOBAL_RVA";
    private const string AnchorExport = "ACCT_BuildAcctList";
    private const int SearchWindow = 64;
    private static readonly byte[] FallbackLoad = { 0x85, 0xC0, 0x75, 0x05, 0xA1 };

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    public static void Set(IntPtr qaccess, IntPtr db)
    {
        Marshal.WriteInt32(Resolve(qaccess), db.ToInt32());
    }

    public static IntPtr Resolve(IntPtr qaccess)
    {
        var imageSize = ImageSize(qaccess);
        var configured = Environment.GetEnvironmentVariable(OverrideVariable);
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var text = configured.Trim();
            if (text.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) text = text.Substring(2);
            int rva;
            if (!int.TryParse(text, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out rva))
                throw new InvalidOperationException(OverrideVariable + " is not a hexadecimal RVA: " + configured);
            if (!IsDataSlot(rva, imageSize))
                throw new InvalidOperationException(string.Format(
                    "{0}=0x{1:x} lies outside qaccess.dll (image size 0x{2:x})", OverrideVariable, rva, imageSize));
            Console.WriteLine(string.Format("qaccess database global: rva=0x{0:x} source={1}", rva, OverrideVariable));
            return IntPtr.Add(qaccess, rva);
        }

        var code = GetProcAddress(qaccess, AnchorExport);
        if (code == IntPtr.Zero) throw new InvalidOperationException("missing export " + AnchorExport);
        for (var i = 0; i + FallbackLoad.Length + 4 <= SearchWindow; i++)
        {
            if (!Matches(code, i)) continue;
            var found = Marshal.ReadInt32(code, i + FallbackLoad.Length) - qaccess.ToInt32();
            if (!IsDataSlot(found, imageSize)) continue;
            Console.WriteLine(string.Format("qaccess database global: rva=0x{0:x} source={1}+0x{2:x}", found, AnchorExport, i));
            return IntPtr.Add(qaccess, found);
        }
        throw new InvalidOperationException(
            "Could not locate qaccess.dll's current-database global from " + AnchorExport +
            " in this Quicken build. Set " + OverrideVariable + " to its RVA (hex) to proceed.");
    }

    private static bool Matches(IntPtr code, int offset)
    {
        for (var j = 0; j < FallbackLoad.Length; j++)
            if (Marshal.ReadByte(code, offset + j) != FallbackLoad[j]) return false;
        return true;
    }

    private static bool IsDataSlot(int rva, int imageSize)
    {
        return rva > 0 && rva + 4 <= imageSize && rva % 4 == 0;
    }

    private static int ImageSize(IntPtr module)
    {
        // IMAGE_NT_HEADERS32.OptionalHeader.SizeOfImage, read from the mapped image.
        var ntHeaders = Marshal.ReadInt32(module, 0x3c);
        return Marshal.ReadInt32(module, ntHeaders + 0x50);
    }
}
