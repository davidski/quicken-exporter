using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;

internal static class ExtractQdbFinancial
{
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr LoadLibrary(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll")]
    private static extern uint SetErrorMode(uint mode);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate IntPtr OpenDb(IntPtr path);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int NumItems(IntPtr db, uint type);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate IntPtr GetSpec(uint type);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate IntPtr GetItem2(IntPtr db, uint type, uint mode, uint key, uint arg5, uint size, IntPtr buffer, IntPtr output);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int GetString(IntPtr db, uint id, IntPtr output);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int CloseDb(IntPtr db);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int OpenPriceHistory(IntPtr context, IntPtr db, IntPtr pathBase);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate ushort MaximumSecurityRef(IntPtr context, IntPtr db);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int SecurityNameAndSymbol(IntPtr context, IntPtr db, IntPtr name, IntPtr symbol, uint securityRef, uint nameLength);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int CountQuotes(uint securityRef, uint startDate, uint endDate);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int GetQuotes(uint securityRef, uint startDate, uint endDate, int adjust, int maximum, IntPtr quotes);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate double PriceValueToDouble(long price);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int BuildAcctList(uint mask, uint db);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int CountAccounts(uint mask, uint db);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate ushort NthAcctHandle(uint index, uint db, uint mask);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int GetAcctInfo(uint flags, uint db, uint account, IntPtr info);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int GetAcctType(uint flags, uint db, uint account);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int GetAcctSubType(uint flags, uint db, uint account);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate bool AccountTypePredicate(ushort account);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate bool AccountFlag(uint flags, uint db, uint account);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate bool InfoFlag(IntPtr info);

    private static T Load<T>(IntPtr module, string name) where T : class
    {
        var p = GetProcAddress(module, name);
        if (p == IntPtr.Zero) throw new InvalidOperationException("missing export " + name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(p, typeof(T));
    }

    public static int Main(string[] args)
    {
        if (args.Length < 2 || args.Length > 3 ||
            (args.Length == 3 && args[2] != QdbPassword.PasswordStdinArgument))
            throw new ArgumentException(
                "usage: Extract-QdbFinancial.exe input.qdf output-dir [--password-stdin]"
            );
        // Keep unattended exports non-interactive if a native DLL reports an
        // unexpected fault. The process still returns nonzero to PowerShell.
        SetErrorMode(0x8003); // SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        Directory.CreateDirectory(args[1]);
        // qdb.dll imports callbacks and storage helpers from qwutil; load it
        // first to mirror Quicken's module initialization order.
        var util = LoadLibrary("QWUTIL.dll");
        if (util == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary QWUTIL.dll failed");
        var module = LoadLibrary("QDB.dll");
        if (module == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary QDB.dll failed");
        var access = LoadLibrary("qaccess.dll");
        if (access == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary qaccess.dll failed");
        var open = Load<OpenDb>(module, "QDBOpenDB");
        var openNoPassword = Load<OpenDb>(module, "_QDBNet_OpenDBNoPassword@4");
        var num = Load<NumItems>(module, "QDBNumItems");
        var spec = Load<GetSpec>(module, "QDBGetItemSpec");
        var get = Load<GetItem2>(module, "QDB2GetItem");
        var close = Load<CloseDb>(module, "QDBCloseDB");
        var bytes = System.Text.Encoding.Default.GetBytes(args[0] + "\0");
        var path = Marshal.AllocHGlobal(bytes.Length);
        Marshal.Copy(bytes, 0, path, bytes.Length);
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
                ExtractType(db, num, spec, get, 0x86, args[1]);
                ExtractType(db, num, spec, get, 0x8e, args[1]);
                ExtractType(db, num, spec, get, 0x50, args[1]);
                ExtractType(db, num, spec, get, 0x80, args[1]);
                ExtractType(db, num, spec, get, 0x38, args[1]);
                ExtractType(db, num, spec, get, 0x51, args[1]);
                ExtractType(db, num, spec, get, 0x63, args[1]);
                ExtractType(db, num, spec, get, 0x67, args[1]);
                ExtractType(db, num, spec, get, 0x96, args[1]);
                ExtractType(db, num, spec, get, 0x99, args[1]);
                ExtractType(db, num, spec, get, 0x9c, args[1]);
                ExtractType(db, num, spec, get, 0xcb, args[1]);
                ExtractType(db, num, spec, get, 0xad, args[1]);
                ExtractType(db, num, spec, get, 0x13c, args[1]);
                ExtractType(db, num, spec, get, 0x134, args[1]);
                ExtractAccountStatus(db, access, args[1]);
                // Transaction-family companion records.  These are kept in the
                // same decoded directory so the category/split investigation can
                // correlate them with the canonical 0x13c rows.
                foreach (var companion in new uint[] {
                    0x04f, 0x054, 0x055, 0x056, 0x05a, 0x05b, 0x05d, 0x069,
                    0x079, 0x07f, 0x09d, 0x0a0, 0x0a2, 0x0a3, 0x0a4,
                    0x0a5, 0x0a6, 0x0a7, 0x0a8, 0x0a9, 0x0aa, 0x0ab, 0x0ac,
                    0x0ae, 0x0c2, 0x0c6, 0x0cc, 0x0cd, 0x0e5, 0x0e7, 0x0e9,
                    0x0eb, 0x0ee, 0x0ef, 0x0f0, 0x0f1, 0x0f2, 0x0f3, 0x0f4,
                    0x0f5,
                    0xb1, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8, 0xb9,
                    0xba, 0xbb, 0xbc, 0xbd, 0xbe, 0xf7, 0xf8, 0xf9,
                    0x113, 0x123, 0x127, 0x128, 0x12b, 0x12c, 0x12d, 0x12e,
                    0x135, 0x136, 0x137, 0x138, 0x139, 0x13a, 0x13b
                }) ExtractType(db, num, spec, get, companion, args[1]);
                ExtractTransactionStrings(db, args[1]);
                ExtractSecurityPrices(db, access, args[0], args[1]);
            }
            finally { close(db); }
            return 0;
        }
        finally { Marshal.FreeHGlobal(path); }
    }

    private static void ExtractAccountStatus(IntPtr db, IntPtr access, string outputDir)
    {
        // The account catalog APIs use the qaccess current-database global,
        // while the status accessors also accept the explicit QDB handle.
        Marshal.WriteInt32(IntPtr.Add(access, 0x2b9efc), db.ToInt32());
        var build = Load<BuildAcctList>(access, "ACCT_BuildAcctList");
        var countAccounts = Load<CountAccounts>(access, "ACCT_CountAccounts");
        var nth = Load<NthAcctHandle>(access, "_ACCT_GetNthAcctHandle@12");
        var getInfo = Load<GetAcctInfo>(access, "ACCT_GetAcctInfoFromHandle");
        var getAcctType = Load<GetAcctType>(access, "ACCT_GetAcctType");
        var getAcctSubType = Load<GetAcctSubType>(access, "ACCT_GetSubType");
        var isClosed = Load<AccountFlag>(access, "_ACCT_IsClosed@12");
        var isSeparate = Load<AccountFlag>(access, "ACCT_IsHidden");
        var isHiddenInBar = Load<AccountFlag>(access, "ACCT_IsHiddenInBar");
        var isHiddenInList = Load<AccountFlag>(access, "_ACCT_IsHiddenInList@12");
        // These flag accessors are retained as a native validation path.  The
        // three-argument accessors above are the values written to the sidecar.
        var closedFlag = Load<InfoFlag>(access, "_ACCT_IsClosedFlag@4");
        var separateFlag = Load<InfoFlag>(access, "ACCT_IsHiddenFlag");
        var hiddenBarFlag = Load<InfoFlag>(access, "ACCT_IsHiddenInBarFlag");
        var hiddenListFlag = Load<InfoFlag>(access, "_ACCT_IsHiddenInListFlag@4");
        if (build(0x3f, unchecked((uint)db.ToInt32())) == 0)
            throw new InvalidOperationException("ACCT_BuildAcctList failed");
        var count = countAccounts(0x3f, unchecked((uint)db.ToInt32()));
        if (count < 0 || count > 10000)
            throw new InvalidOperationException("unexpected account count: " + count);
        var info = Marshal.AllocHGlobal(0x1000);
        try
        {
            using (var writer = new StreamWriter(
                Path.Combine(outputDir, "qdb-account-status.tsv"), false,
                new System.Text.UTF8Encoding(false)))
            {
                writer.WriteLine("qdb_handle\taccount_type\taccount_subtype\tis_closed\tis_separate\tis_hidden_in_bar\tis_hidden_in_list");
                for (uint index = 0; index < (uint)count; index++)
                {
                    var handle = nth(0x3f, unchecked((uint)db.ToInt32()), index);
                    for (var offset = 0; offset < 0x1000; offset++) Marshal.WriteByte(info, offset, 0);
                    var result = getInfo(0, unchecked((uint)db.ToInt32()), handle, info);
                    if (result == 0) continue;
                    var accountType = getAcctType(0, 0, handle);
                    if (accountType < 0 || accountType > 255)
                        throw new InvalidOperationException("unexpected account type for handle " + handle);
                    // ACCT_GetSubType returns a legacy packed value whose low byte is the subtype;
                    // subtype 3 is House and subtype 5 is Vehicle in the observed UI.
                    var accountSubtype = getAcctSubType(0, 0, handle) & 0xff;
                    var closed = isClosed(0, unchecked((uint)db.ToInt32()), handle);
                    var separate = isSeparate(0, unchecked((uint)db.ToInt32()), handle);
                    var hiddenBar = isHiddenInBar(0, unchecked((uint)db.ToInt32()), handle);
                    var hiddenList = isHiddenInList(0, unchecked((uint)db.ToInt32()), handle);
                    // Verify that the high-level accessors agree with the raw
                    // status bits.  A mismatch indicates a native ABI/version
                    // change and must not silently corrupt the export.
                    if (closed != closedFlag(info) || separate != separateFlag(info) ||
                        hiddenBar != hiddenBarFlag(info) || hiddenList != hiddenListFlag(info))
                        throw new InvalidOperationException("account status flag mismatch for handle " + handle);
                    writer.WriteLine(
                        handle.ToString(CultureInfo.InvariantCulture) + "\t" +
                        accountType.ToString(CultureInfo.InvariantCulture) + "\t" +
                        accountSubtype.ToString(CultureInfo.InvariantCulture) + "\t" +
                        (closed ? "1" : "0") + "\t" +
                        (separate ? "1" : "0") + "\t" +
                        (hiddenBar ? "1" : "0") + "\t" +
                        (hiddenList ? "1" : "0"));
                }
            }
        }
        finally { Marshal.FreeHGlobal(info); }
    }

    private static string CleanField(string value)
    {
        return (value ?? "").Replace('\t', ' ').Replace('\r', ' ').Replace('\n', ' ');
    }

    private static string QuoteDate(uint word)
    {
        var year = 1900 + (int)((word >> 16) & 0xff);
        var month = (int)((word >> 8) & 0xff);
        var day = (int)(word & 0xff);
        try { return new DateTime(year, month, day).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture); }
        catch (ArgumentOutOfRangeException) { return ""; }
    }

    private static string PriceText(double value, bool optional)
    {
        if (Double.IsNaN(value) || Double.IsInfinity(value) || (optional && value == 0.0)) return "";
        return value.ToString("R", CultureInfo.InvariantCulture);
    }

    // qaccess exposes the security catalog from the open QDB and the embedded
    // .QPH stream through its price-history API.  SIOpenPriceHistory appends
    // ".QPH" itself, so pass the QDF path without its extension; when Quicken
    // structured storage is active this resolves the bundle's .QPH stream.
    private static void ExtractSecurityPrices(IntPtr db, IntPtr access, string qdfPath, string outputDir)
    {
        var openPriceHistory = Load<OpenPriceHistory>(access, "_SIOpenPriceHistory@12");
        var maximumSecurityRef = Load<MaximumSecurityRef>(access, "SIGetMaximumSecurityRef");
        var securityNameAndSymbol = Load<SecurityNameAndSymbol>(access, "_SIGetSecurityNameAndSymbol@24");
        var countQuotes = Load<CountQuotes>(access, "SICountQuotes");
        var getQuotes = Load<GetQuotes>(access, "SIGetQuotes");
        var priceValueToDouble = Load<PriceValueToDouble>(access, "PriceValueToDouble");
        var basePath = Path.Combine(
            Path.GetDirectoryName(qdfPath) ?? "",
            Path.GetFileNameWithoutExtension(qdfPath)
        );
        var baseBytes = System.Text.Encoding.Default.GetBytes(basePath + "\0");
        var basePointer = Marshal.AllocHGlobal(baseBytes.Length);
        Marshal.Copy(baseBytes, 0, basePointer, baseBytes.Length);
        try
        {
            if (openPriceHistory(IntPtr.Zero, db, basePointer) == 0)
                throw new InvalidOperationException("SIOpenPriceHistory could not open the embedded .QPH stream");
        }
        finally { Marshal.FreeHGlobal(basePointer); }

        var name = Marshal.AllocHGlobal(256);
        var symbol = Marshal.AllocHGlobal(64);
        var securityCount = 0;
        var quoteCount = 0;
        var utf8 = new System.Text.UTF8Encoding(false);
        try
        {
            using (var securities = new StreamWriter(Path.Combine(outputDir, "qdb-securities.tsv"), false, utf8))
            using (var prices = new StreamWriter(Path.Combine(outputDir, "qdb-price-history.tsv"), false, utf8))
            {
                securities.WriteLine("qdb_security_ref\tname\tsymbol");
                prices.WriteLine("qdb_security_ref\tprice_date\tprice\thigh\tlow\tvolume");
                var maximum = maximumSecurityRef(IntPtr.Zero, db);
                for (uint securityRef = 1; securityRef <= maximum; securityRef++)
                {
                    for (var index = 0; index < 256; index++) Marshal.WriteByte(name, index, 0);
                    for (var index = 0; index < 64; index++) Marshal.WriteByte(symbol, index, 0);
                    if (securityNameAndSymbol(IntPtr.Zero, db, name, symbol, securityRef, 255) == 0)
                        continue;
                    securities.WriteLine(
                        securityRef.ToString(CultureInfo.InvariantCulture) + "\t" +
                        CleanField(Marshal.PtrToStringAnsi(name)) + "\t" +
                        CleanField(Marshal.PtrToStringAnsi(symbol))
                    );
                    securityCount++;

                    var count = countQuotes(securityRef, 0, UInt32.MaxValue);
                    if (count <= 0) continue;
                    if (count > 10000000)
                        throw new InvalidOperationException("unreasonable quote count for security " + securityRef + ": " + count);
                    var quotes = Marshal.AllocHGlobal(checked(count * 32));
                    try
                    {
                        var returned = getQuotes(securityRef, 0, UInt32.MaxValue, 0, count, quotes);
                        if (returned < 0 || returned > count)
                            throw new InvalidOperationException("invalid quote result for security " + securityRef + ": " + returned);
                        for (var index = 0; index < returned; index++)
                        {
                            var offset = index * 32;
                            var dateWord = unchecked((uint)Marshal.ReadInt32(quotes, offset));
                            var date = QuoteDate(dateWord);
                            if (date.Length == 0) continue;
                            var price = priceValueToDouble(Marshal.ReadInt64(quotes, offset + 4));
                            var high = priceValueToDouble(Marshal.ReadInt64(quotes, offset + 12));
                            var low = priceValueToDouble(Marshal.ReadInt64(quotes, offset + 20));
                            var volume = Marshal.ReadInt32(quotes, offset + 28);
                            prices.WriteLine(
                                securityRef.ToString(CultureInfo.InvariantCulture) + "\t" + date + "\t" +
                                PriceText(price, false) + "\t" + PriceText(high, true) + "\t" +
                                PriceText(low, true) + "\t" +
                                (volume == 0 ? "" : volume.ToString(CultureInfo.InvariantCulture))
                            );
                            quoteCount++;
                        }
                    }
                    finally { Marshal.FreeHGlobal(quotes); }
                }
            }
        }
        finally
        {
            Marshal.FreeHGlobal(symbol);
            Marshal.FreeHGlobal(name);
        }
        Console.WriteLine("security prices: securities=" + securityCount + " quotes=" + quoteCount);
    }

    private static void ExtractType(IntPtr db, NumItems num, GetSpec spec, GetItem2 get, uint type, string outputDir)
    {
        var count = num(db, type);
        var itemSpec = spec(type);
        var size = itemSpec == IntPtr.Zero ? 0 : Marshal.ReadInt32(itemSpec, 0x20);
        Console.WriteLine(string.Format("type=0x{0:x} count={1} size={2}", type, count, size));
        if (count <= 0 || size <= 0 || size > 0x100000) return;
        var buffer = Marshal.AllocHGlobal(size);
        var output = Marshal.AllocHGlobal(4);
        var file = Path.Combine(outputDir, string.Format("qdb-type-{0:x3}.bin", type));
        try
        {
            using (var stream = new FileStream(file, FileMode.Create, FileAccess.Write, FileShare.Read))
            {
                var header = BitConverter.GetBytes(size);
                stream.Write(header, 0, header.Length);
                header = BitConverter.GetBytes(count);
                stream.Write(header, 0, header.Length);
                for (uint key = 1; key <= (uint)count; key++)
                {
                    for (int i = 0; i < size; i++) Marshal.WriteByte(buffer, i, 0);
                    Marshal.WriteInt32(output, 0, 0);
                    var item = get(db, type, 0, key, 0, (uint)size, buffer, output);
                    var row = new byte[size];
                    Marshal.Copy(buffer, row, 0, size);
                    stream.Write(row, 0, row.Length);
                    if (key <= 3 || (key % 5000) == 0) Console.WriteLine(string.Format("  key={0} item=0x{1:x} out={2}", key, item.ToInt64(), Marshal.ReadInt32(output)));
                }
            }
        }
        finally { Marshal.FreeHGlobal(output); Marshal.FreeHGlobal(buffer); }
    }

    // The decoded transaction record stores references into QDB's string pool.
    // Offset 0 is the record's primary string/id slot; offset 0xa0 is the
    // category/security/related-string slot used by newer records.  Preserve
    // the reversible id-to-text mapping so Python can distinguish category
    // markers (Quicken stores them with a leading '=') from ordinary strings.
    private static void ExtractTransactionStrings(IntPtr db, string outputDir)
    {
        var ids = new System.Collections.Generic.HashSet<uint>();
        foreach (var sourceName in new[] {
            "qdb-type-13c.bin", "qdb-type-086.bin", "qdb-type-0f7.bin", "qdb-type-0f8.bin",
            "qdb-type-096.bin",
            "qdb-type-0f9.bin", "qdb-type-0b1.bin", "qdb-type-0b2.bin", "qdb-type-0b3.bin",
            "qdb-type-0b4.bin", "qdb-type-0b5.bin", "qdb-type-0b6.bin", "qdb-type-113.bin",
            "qdb-type-123.bin", "qdb-type-127.bin", "qdb-type-128.bin", "qdb-type-135.bin"
        })
        {
            var source = Path.Combine(outputDir, sourceName);
            if (!File.Exists(source)) continue;
            var data = File.ReadAllBytes(source); if (data.Length < 8) continue;
            var size = BitConverter.ToInt32(data, 0); var count = BitConverter.ToInt32(data, 4);
            if (size < 4 || count < 0 || 8L + (long)size * count > data.Length) continue;
            for (int i = 0; i < count; i++)
            {
                var off = 8 + i * size;
                for (int field = 0; field + 4 <= size; field += 4)
                    ids.Add(BitConverter.ToUInt32(data, off + field));
            }
        }
        // Current downloaded transactions have an ordinally paired 0x0b7
        // companion.  Its normalized/user-facing payee and downloaded payee
        // string IDs are stored at 0x63 and 0x67 respectively.
        var payeeSource = Path.Combine(outputDir, "qdb-type-0b7.bin");
        if (File.Exists(payeeSource))
        {
            var payeeData = File.ReadAllBytes(payeeSource);
            if (payeeData.Length >= 8)
            {
                var payeeSize = BitConverter.ToInt32(payeeData, 0);
                var payeeCount = BitConverter.ToInt32(payeeData, 4);
                if (payeeSize >= 0x6b && payeeCount >= 0 &&
                    8L + (long)payeeSize * payeeCount <= payeeData.Length)
                    for (int i = 0; i < payeeCount; i++)
                    {
                        var off = 8 + i * payeeSize;
                        ids.Add(BitConverter.ToUInt32(payeeData, off + 0x63));
                        ids.Add(BitConverter.ToUInt32(payeeData, off + 0x67));
                    }
            }
        }
        // Register transactions hold the payee Quicken actually displays at
        // offset 0x63, after automatic and manual renaming.
        var registerSource = Path.Combine(outputDir, "qdb-type-0f7.bin");
        if (File.Exists(registerSource))
        {
            var registerData = File.ReadAllBytes(registerSource);
            if (registerData.Length >= 8)
            {
                var registerSize = BitConverter.ToInt32(registerData, 0);
                var registerCount = BitConverter.ToInt32(registerData, 4);
                if (registerSize >= 0x67 && registerCount >= 0 &&
                    8L + (long)registerSize * registerCount <= registerData.Length)
                    for (int i = 0; i < registerCount; i++)
                        ids.Add(BitConverter.ToUInt32(registerData, 8 + i * registerSize + 0x63));
            }
        }
        var getString = Load<GetString>(LoadLibrary("QDB.dll"), "QDBGetString");
        var buffer = Marshal.AllocHGlobal(2048);
        try
        {
            using (var writer = new StreamWriter(Path.Combine(outputDir, "qdb-string-map.tsv"), false, System.Text.Encoding.UTF8))
            {
                foreach (var id in ids)
                {
                    if (id == 0) continue;
                    for (int i = 0; i < 2048; i++) Marshal.WriteByte(buffer, i, 0);
                    if (getString(db, id, buffer) == 0) continue;
                    var bytes = new byte[2048]; Marshal.Copy(buffer, bytes, 0, bytes.Length);
                    var text = System.Text.Encoding.Default.GetString(bytes).Split('\0')[0];
                    if (text.Length > 0) writer.WriteLine(id.ToString() + "\t" + text.Replace("\r", " ").Replace("\n", " "));
                }
            }
        }
        finally { Marshal.FreeHGlobal(buffer); }
    }
}
