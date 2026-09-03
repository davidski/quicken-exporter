using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class ExtractQdbAccountMap
{
    private const uint TxType = 0xf7;
    private const uint IndexType = 0x134;
    private const int RecordSize = 211;

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr LoadLibrary(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll")]
    private static extern uint SetErrorMode(uint mode);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate IntPtr OpenDb(IntPtr path);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int NumTxItems(IntPtr db, ushort account, uint type);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate IntPtr GetTxItem(IntPtr db, ushort account, uint type, uint keyType, uint key, IntPtr item);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int CloseDb(IntPtr db);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate void RegCtor(IntPtr self, ushort account, uint key);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate void RegDtor(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate uint MemoRef(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate bool GetText(IntPtr self, IntPtr buffer, int length);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate int GetClearStatus(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate int NumSplits(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate void GetSplit(IntPtr self, int index, IntPtr split);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate bool BoolValue(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate uint SecurityValue(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate long InvestmentAmount(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate uint PairValue(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate ushort TransferAccount(IntPtr self, bool load);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate ushort InvestmentType(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate long InvestBalance(uint first, uint second, uint account, uint index, IntPtr output);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate int BuildAcctList(uint mask, uint db);
    [UnmanagedFunctionPointer(CallingConvention.ThisCall)] private delegate void ValueStruct(IntPtr self, IntPtr result);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] private delegate double ValueToDouble(long value);

    private sealed class SplitData
    {
        public uint Category;
        public uint Transfer;
        public uint Memo;
        public long Amount;
        public string RawHex;
    }

    private sealed class InvestmentData
    {
        public uint Security;
        public double Shares;
        public double Price;
        public long Amount;
        public long TransactionAmount;
        public uint BackfillPair;
        public bool IsBackfillCash;
        public uint TransferQid;
        public ushort XferAccount;
        public string StartLocation;
        public string DestinationLocation;
        public long NativeCashBalance;
        public ushort Type;
        public string TypeName;
        public bool IsCash;
    }

    private sealed class MemoApi
    {
        private readonly RegCtor ctor;
        private readonly RegDtor dtor;
        private readonly MemoRef memoRef;
        private readonly GetClearStatus getClearStatus;
    private readonly GetText getMemo;
    private readonly GetText getPayee;
    private readonly GetText getCheckNumText;
        private readonly NumSplits numSplits;
        private readonly GetSplit getSplit;
        private readonly BoolValue isInvestment;
        private readonly BoolValue isInvestmentCash;
        private readonly SecurityValue getSecurity;
        private readonly InvestmentAmount getInvestmentAmount;
        private readonly InvestmentAmount getTransactionAmount;
        private readonly PairValue getBackfillPair;
        private readonly BoolValue isBackfillCash;
        private readonly PairValue getTransferQid;
        private readonly TransferAccount getXferAccount;
        private readonly GetText getStartLocation;
        private readonly GetText getDestinationLocation;
        private readonly InvestBalance getInvestBalance;
        private readonly IntPtr balanceBuffer = Marshal.AllocHGlobal(16);
        private readonly InvestmentType getInvestmentType;
        private readonly GetText getInvestmentTypeName;
        private readonly ValueStruct getShares;
        private readonly ValueStruct getPrice;
        private readonly ValueToDouble sharesToDouble;
        private readonly ValueToDouble priceToDouble;
        private readonly IntPtr self = Marshal.AllocHGlobal(0x10000);
        private readonly IntPtr buffer = Marshal.AllocHGlobal(4096);

        private MemoApi(IntPtr module)
        {
            ctor = Load<RegCtor>(module, "??0RegTransaction@@QAE@GI@Z");
            dtor = Load<RegDtor>(module, "??1RegTransaction@@UAE@XZ");
            getClearStatus = Load<GetClearStatus>(module, "?GetClearStatus@RegTransaction@@QAEHXZ");
            memoRef = Load<MemoRef>(module, "?GetMemoRef@Transaction@@QAEKXZ");
            getMemo = Load<GetText>(module, "?GetMemo@Transaction@@QAE_NPADH@Z");
            getPayee = Load<GetText>(module, "?GetPayee@Transaction@@QAEPADPADH@Z");
            getCheckNumText = Load<GetText>(module, "?GetCheckNumText@Transaction@@QAE_NPADH@Z");
            numSplits = Load<NumSplits>(module, "?GetNumSplits@Transaction@@QAEHXZ");
            getSplit = Load<GetSplit>(module, "?GetSplit@Transaction@@QAEXHPAUQDB_SPLIT_Q03v1_TYPE@@@Z");
            isInvestment = Load<BoolValue>(module, "?IsInvestmentTxn@Transaction@@QAE_NXZ");
            isInvestmentCash = Load<BoolValue>(module, "?IsInvestmentCashTxn@Transaction@@QAE_NXZ");
            getSecurity = Load<SecurityValue>(module, "?GetSecurity@Transaction@@QAEKXZ");
            getInvestmentAmount = Load<InvestmentAmount>(module, "?GetInvestmentAmount@Transaction@@UAE_JXZ");
            getTransactionAmount = Load<InvestmentAmount>(module, "?GetAmount@Transaction@@UAE_JXZ");
            // These methods are implemented by RegTransaction (the object
            // constructed above), and identify Quicken's paired cash leg for
            // backfilled/opening transactions.  They are intentionally kept
            // in the native sidecar so analytics can reconcile transfer legs
            // without guessing from amounts or dates.
            getBackfillPair = Load<PairValue>(module, "?GetBackfillPair@RegTransaction@@QAEKXZ");
            isBackfillCash = Load<BoolValue>(module, "?IsBackfillCash@RegTransaction@@QAE_NXZ");
            getTransferQid = Load<PairValue>(module, "?GetTransferQid@RegTransaction@@QAEKXZ");
            getXferAccount = Load<TransferAccount>(module, "?GetXferAccount@Transaction@@QAEG_N@Z");
            getStartLocation = Load<GetText>(module, "?GetStartLocation@RegTransaction@@QAE_NPADI@Z");
            getDestinationLocation = Load<GetText>(module, "?GetDestinationLocation@RegTransaction@@QAE_NPADI@Z");
            getInvestBalance = Load<InvestBalance>(module, "GetInvestBalance");
            getInvestmentType = Load<InvestmentType>(module, "?GetInvTxnType@Transaction@@QAEFXZ");
            getInvestmentTypeName = Load<GetText>(module, "?GetInvTxnTypeName@Transaction@@QAE_NPADH@Z");
            getShares = Load<ValueStruct>(module, "?GetShares@Transaction@@QAE?ATSHARE_TYPE@@XZ");
            getPrice = Load<ValueStruct>(module, "?GetPrice@Transaction@@QAE?ATPRICE_TYPE@@XZ");
            sharesToDouble = Load<ValueToDouble>(module, "ShareValueToDouble");
            priceToDouble = Load<ValueToDouble>(module, "PriceValueToDouble");
        }

        public static MemoApi Open(IntPtr db)
        {
            var module = LoadLibrary("qaccess.dll");
            if (module == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary qaccess.dll failed");
            // qaccess's transaction methods use this module global as the QDB handle.
            Marshal.WriteInt32(IntPtr.Add(module, 0x2b9efc), db.ToInt32());
            var build = Load<BuildAcctList>(module, "ACCT_BuildAcctList");
            build(0x3f, unchecked((uint)db.ToInt32()));
            return new MemoApi(module);
        }

        public string[] Read(ushort account, uint key)
        {
            for (var i = 0; i < 0x10000; i++) Marshal.WriteByte(self, i, 0);
            for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
            // FillBaseTxnInfoByIndex increments the key for account-index records.
            ctor(self, account, key - 1);
            try
            {
                var reference = memoRef(self);
                if (reference == 0) return null;
                var memoOk = getMemo(self, buffer, 4096);
                var memo = memoOk ? (Marshal.PtrToStringAnsi(buffer) ?? "") : "";
                for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                var payee = getPayee(self, buffer, 4096) ? (Marshal.PtrToStringAnsi(buffer) ?? "") : "";
                for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                var checkNumber = getCheckNumText(self, buffer, 4096)
                    ? (Marshal.PtrToStringAnsi(buffer) ?? "")
                    : "";
                return new[] { reference.ToString(), memo, payee, checkNumber };
            }
            finally { dtor(self); }
        }

        public System.Collections.Generic.List<SplitData> ReadSplits(ushort account, uint key)
        {
            var result = new System.Collections.Generic.List<SplitData>();
            for (var i = 0; i < 0x10000; i++) Marshal.WriteByte(self, i, 0);
            ctor(self, account, key - 1);
            try
            {
                var count = numSplits(self);
                for (var index = 0; index < count; index++)
                {
                    for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                    getSplit(self, index, buffer);
                    var raw = new byte[64];
                    Marshal.Copy(buffer, raw, 0, raw.Length);
                    result.Add(new SplitData {
                        Category = unchecked((uint)Marshal.ReadInt32(buffer, 0)),
                        Transfer = unchecked((uint)Marshal.ReadInt32(buffer, 4)),
                        Memo = unchecked((uint)Marshal.ReadInt32(buffer, 8)),
                        Amount = Marshal.ReadInt64(buffer, 12),
                        RawHex = BitConverter.ToString(raw).Replace("-", "")
                    });
                }
            }
            finally { dtor(self); }
            return result;
        }

        public string ReadCheckNumber(ushort account, uint key)
        {
            for (var i = 0; i < 0x10000; i++) Marshal.WriteByte(self, i, 0);
            for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
            ctor(self, account, key - 1);
            try
            {
                return getCheckNumText(self, buffer, 4096)
                    ? (Marshal.PtrToStringAnsi(buffer) ?? "")
                    : "";
            }
            finally { dtor(self); }
        }
        public int ReadClearStatus(ushort account, uint key)
        {
            for (var i = 0; i < 0x10000; i++) Marshal.WriteByte(self, i, 0);
            ctor(self, account, key - 1);
            try
            {
                return getClearStatus(self);
            }
            finally { dtor(self); }
        }


        public InvestmentData ReadInvestment(ushort account, uint key)
        {
            for (var i = 0; i < 0x10000; i++) Marshal.WriteByte(self, i, 0);
            ctor(self, account, key - 1);
            try
            {
                if (!isInvestment(self)) return null;
                var sharesBuffer = Marshal.AllocHGlobal(8);
                var priceBuffer = Marshal.AllocHGlobal(8);
                try
                {
                    for (var i = 0; i < 8; i++)
                    {
                        Marshal.WriteByte(sharesBuffer, i, 0);
                        Marshal.WriteByte(priceBuffer, i, 0);
                    }
                    getShares(self, sharesBuffer);
                    getPrice(self, priceBuffer);
                    for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                    var typeName = getInvestmentTypeName(self, buffer, 4096)
                        ? (Marshal.PtrToStringAnsi(buffer) ?? "")
                        : "";
                    for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                    var startLocation = getStartLocation(self, buffer, 4096)
                        ? (Marshal.PtrToStringAnsi(buffer) ?? "")
                        : "";
                    for (var i = 0; i < 4096; i++) Marshal.WriteByte(buffer, i, 0);
                    var destinationLocation = getDestinationLocation(self, buffer, 4096)
                        ? (Marshal.PtrToStringAnsi(buffer) ?? "")
                        : "";
                    var nativeCashBalance = getInvestBalance(
                        0, 0, account, key - 1, balanceBuffer);
                    return new InvestmentData {
                        Security = getSecurity(self),
                        Shares = sharesToDouble(Marshal.ReadInt64(sharesBuffer)),
                        Price = priceToDouble(Marshal.ReadInt64(priceBuffer)),
                        Amount = getInvestmentAmount(self),
                        TransactionAmount = getTransactionAmount(self),
                        BackfillPair = getBackfillPair(self),
                        IsBackfillCash = isBackfillCash(self),
                        TransferQid = getTransferQid(self),
                        XferAccount = getXferAccount(self, false),
                        StartLocation = startLocation,
                        DestinationLocation = destinationLocation,
                        NativeCashBalance = nativeCashBalance,
                        Type = getInvestmentType(self),
                        TypeName = typeName,
                        IsCash = isInvestmentCash(self),
                    };
                }
                finally
                {
                    Marshal.FreeHGlobal(priceBuffer);
                    Marshal.FreeHGlobal(sharesBuffer);
                }
            }
            finally { dtor(self); }
        }
    }

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
                "usage: Extract-QdbAccountMap.exe input.qdf output.bin [--password-stdin]"
            );
        SetErrorMode(0x8003);
        var outputPath = Path.GetFullPath(args[1]);
        Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
        LoadLibrary("QWUTIL.dll");
        var module = LoadLibrary("QDB.dll");
        if (module == IntPtr.Zero) throw new InvalidOperationException("LoadLibrary QDB.dll failed");
        var open = Load<OpenDb>(module, "QDBOpenDB");
        var openNoPassword = Load<OpenDb>(module, "_QDBNet_OpenDBNoPassword@4");
        var num = Load<NumTxItems>(module, "QDBNumTxItems");
        var get = Load<GetTxItem>(module, "QDBGetTxItem");
        var close = Load<CloseDb>(module, "QDBCloseDB");
        var bytes = Encoding.Default.GetBytes(args[0] + "\0");
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
            try {
                var memoApi = MemoApi.Open(db);
                Extract(db, num, get, outputPath, TxType, memoApi);
                var accountMapPath = Path.Combine(
                    Path.GetDirectoryName(outputPath), "qdb-account-map-134.bin");
                Extract(db, num, get, accountMapPath, IndexType);
                ExtractCanonicalCheckNumbers(
                    accountMapPath,
                    Path.Combine(Path.GetDirectoryName(outputPath), "qdb-canonical-check-numbers.tsv"),
                    memoApi);
            }
            finally { close(db); }
            return 0;
        }
        finally { Marshal.FreeHGlobal(path); }
    }

    private static void ExtractCanonicalCheckNumbers(
        string accountMapPath, string outputPath, MemoApi memoApi)
    {
        using (var stream = new FileStream(accountMapPath, FileMode.Open, FileAccess.Read, FileShare.Read))
        using (var reader = new BinaryReader(stream))
        using (var writer = new StreamWriter(outputPath, false, new UTF8Encoding(false)))
        {
            var magic = reader.ReadBytes(4);
            var version = reader.ReadUInt32();
            var recordSize = reader.ReadUInt32();
            var count = reader.ReadUInt32();
            if (magic.Length != 4 || Encoding.ASCII.GetString(magic) != "QATM" ||
                version != 1 || recordSize != RecordSize)
                throw new InvalidDataException("unexpected canonical account-map header");
            writer.WriteLine("account\tkey\tqdb_internal_id\tcheck_number");
            for (uint index = 0; index < count; index++)
            {
                var account = reader.ReadUInt32();
                var key = reader.ReadUInt32();
                var row = reader.ReadBytes((int)recordSize);
                if (row.Length != recordSize)
                    throw new InvalidDataException("truncated canonical account-map row");
                var internalId = BitConverter.ToUInt32(row, 0);
                var checkNumber = memoApi.ReadCheckNumber((ushort)account, key);
                if (!string.IsNullOrWhiteSpace(checkNumber))
                    writer.WriteLine(string.Join("\t", account, key, internalId, Clean(checkNumber)));
            }
        }
    }

    private static void Extract(IntPtr db, NumTxItems num, GetTxItem get, string outputPath, uint txType, MemoApi memoApi = null)
    {
        var handles = new System.Collections.Generic.List<ushort>();
        var total = 0;
        for (uint raw = 0; raw < 65535; raw++)
        {
            var handle = (ushort)raw;
            var count = num(db, handle, txType);
            if (count > 0) { handles.Add(handle); total += count; }
        }
        Console.WriteLine(string.Format("type=0x{0:x} handles={1} rows={2}", txType, handles.Count, total));
        var item = Marshal.AllocHGlobal(RecordSize);
        try
        {
            using (var stream = new FileStream(outputPath, FileMode.Create, FileAccess.Write, FileShare.Read))
            using (var writer = new BinaryWriter(stream))
            using (var clearStatusWriter = memoApi == null ? null : new StreamWriter(Path.Combine(Path.GetDirectoryName(outputPath), "qdb-register-clear-status.tsv"), false, new UTF8Encoding(false)))
            using (var memoWriter = memoApi == null ? null : new StreamWriter(Path.Combine(Path.GetDirectoryName(outputPath), "qdb-register-memo.tsv"), false, Encoding.UTF8))
            using (var splitWriter = memoApi == null ? null : new StreamWriter(Path.Combine(Path.GetDirectoryName(outputPath), "qdb-register-splits.tsv"), false, Encoding.UTF8))
            using (var investmentWriter = memoApi == null ? null : new StreamWriter(Path.Combine(Path.GetDirectoryName(outputPath), "qdb-investment-transactions.tsv"), false, new UTF8Encoding(false)))
            {
                if (memoWriter != null) memoWriter.WriteLine("register_ref\tmemo_ref\tmemo\tpayee\tcheck_number\taccount\tkey");
                if (clearStatusWriter != null) clearStatusWriter.WriteLine("register_ref\taccount\tkey\tclear_status");
                if (splitWriter != null) splitWriter.WriteLine("register_ref\tsplit_index\tcategory_handle\ttransfer_handle\tmemo_ref\tamount_cents\traw_hex\taccount\tkey");
                if (investmentWriter != null) investmentWriter.WriteLine("register_ref\taccount\tkey\tsecurity_ref\tshares\tprice\tinvestment_amount\ttransaction_amount\tbackfill_pair\tis_backfill_cash\ttransfer_qid\txfer_account\tstart_location\tdestination_location\tnative_cash_balance\tinv_txn_type\tinv_txn_type_name\tis_cash\ttransaction_date");
                writer.Write(new byte[] { (byte)'Q', (byte)'A', (byte)'T', (byte)'M' });
                writer.Write((uint)1);
                writer.Write((uint)RecordSize);
                writer.Write((uint)0);
                var written = 0;
                foreach (var handle in handles)
                {
                    var count = num(db, handle, txType);
                    for (uint key = 1; key <= (uint)count; key++)
                    {
                        for (var i = 0; i < RecordSize; i++) Marshal.WriteByte(item, i, 0);
                        var result = get(db, handle, txType, 1, key, item);
                        if (result == IntPtr.Zero) continue;
                        var row = new byte[RecordSize];
                        Marshal.Copy(item, row, 0, row.Length);
                        writer.Write((uint)handle);
                        writer.Write(key);
                        writer.Write(row);
                        var registerRef = BitConverter.ToUInt32(row, 0);
                        if (clearStatusWriter != null)
                        {
                            clearStatusWriter.WriteLine(string.Join("\t",
                                registerRef,
                                handle,
                                key,
                                memoApi.ReadClearStatus(handle, key)));
                        }
                        if (memoWriter != null)
                        {
                            var memo = memoApi.Read(handle, key);
                            if (memo != null &&
                                (!string.IsNullOrWhiteSpace(memo[1]) ||
                                 !string.IsNullOrWhiteSpace(memo[3])))
                            {
                                memoWriter.WriteLine(string.Join("\t", registerRef, memo[0], Clean(memo[1]), Clean(memo[2]), Clean(memo[3]), handle, key));
                            }
                            var splits = memoApi.ReadSplits(handle, key);
                            for (var splitIndex = 0; splitIndex < splits.Count; splitIndex++)
                            {
                                var split = splits[splitIndex];
                                splitWriter.WriteLine(string.Join("\t", registerRef, splitIndex, split.Category, split.Transfer, split.Memo, split.Amount, split.RawHex, handle, key));
                            }
                            var investment = memoApi.ReadInvestment(handle, key);
                            if (investment != null)
                            {
                                investmentWriter.WriteLine(string.Join("\t",
                                    registerRef,
                                    handle,
                                    key,
                                    investment.Security,
                                    investment.Shares.ToString("R", CultureInfo.InvariantCulture),
                                    investment.Price.ToString("R", CultureInfo.InvariantCulture),
                                    investment.Amount.ToString(CultureInfo.InvariantCulture),
                                    investment.TransactionAmount.ToString(CultureInfo.InvariantCulture),
                                    investment.BackfillPair,
                                    investment.IsBackfillCash ? "1" : "0",
                                    investment.TransferQid,
                                    investment.XferAccount,
                                    Clean(investment.StartLocation),
                                    Clean(investment.DestinationLocation),
                                    investment.NativeCashBalance,
                                    investment.Type,
                                    Clean(investment.TypeName),
                                    investment.IsCash ? "1" : "0",
                                    RegisterDate(row)
                                ));
                            }
                        }
                        written++;
                    }
                    Console.WriteLine(string.Format("  account={0} rows={1}", handle, count));
                }
                stream.Position = 12;
                writer.Write((uint)written);
            }
        }
        finally { Marshal.FreeHGlobal(item); }
    }

    private static string Clean(string value)
    {
        return (value ?? "").Replace("\t", " ").Replace("\r", " ").Replace("\n", " ");
    }

    private static string RegisterDate(byte[] row)
    {
        try
        {
            return new DateTime(row[8] + 1900, row[7], row[6]).ToString(
                "yyyy-MM-dd", CultureInfo.InvariantCulture
            );
        }
        catch (ArgumentOutOfRangeException) { return ""; }
    }
}
