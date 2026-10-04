using System;
using System.Runtime.InteropServices;

// qaccess.dll keeps the current-database QDB handle in a module global that
// its account and transaction APIs read instead of taking a handle argument.
// If it is not set, the qaccess-backed sidecars (memos, splits, clear status,
// investment transactions) come back empty without any error.
//
// ACCT_SetHQDB(hqdb) is qaccess's exported setter for that global (the only
// code in 27.1.68.31 that writes it), so use it rather than writing the global
// by address. Extract-QdbAccountMap still fails the export if those sidecars
// come back empty.
internal static class QaccessDatabaseGlobal
{
    private const string SetterExport = "ACCT_SetHQDB";

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void SetHqdb(IntPtr db);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);

    public static void Set(IntPtr qaccess, IntPtr db)
    {
        var setter = GetProcAddress(qaccess, SetterExport);
        if (setter == IntPtr.Zero)
            throw new InvalidOperationException(
                "qaccess.dll does not export " + SetterExport + " in this Quicken build.");
        var set = (SetHqdb)Marshal.GetDelegateForFunctionPointer(setter, typeof(SetHqdb));
        set(db);
        Console.WriteLine("qaccess database global: set via " + SetterExport);
    }
}
