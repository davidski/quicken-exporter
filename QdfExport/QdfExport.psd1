@{
    RootModule = 'QdfExport.psm1'
    ModuleVersion = '0.1.0'
    GUID = '2c4e2b0b-94b1-4db8-9ec2-7d4b80f1a0b7'
    Author = 'David Severski'
    Description = 'Read-only Quicken QDF export to SQLite and saved-report reproduction tools'
    PowerShellVersion = '5.1'
    FunctionsToExport = @('Export-QdfFinancial', 'Reproduce-QdfReport', 'Register-QdfScheduledExport')
    CmdletsToExport = @()
    VariablesToExport = @()
    AliasesToExport = @()
}
