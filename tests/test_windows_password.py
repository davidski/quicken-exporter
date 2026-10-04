from pathlib import Path

ROOT = Path(__file__).parents[1]


def read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_password_bridge_is_version_guarded_and_uses_verified_native_entry_points():
    source = read("scripts/windows/QdbPassword.cs")

    assert 'PasswordStdinArgument = "--password-stdin"' in source
    assert "ReadOptionalPassword" in source
    assert "Convert.FromBase64String" in source
    assert 'SupportedQdbVersion = "27.1.68.31"' in source
    assert "PasswordPrimitiveBuilderRva = 0x2eb30" in source
    assert '"_DecryptFileWithPrimitivePassword@8"' in source
    assert "QDF appears to require a data-file password" in source
    assert "The supplied data-file password was rejected" in source
    assert "ZeroMemory(password" in source
    assert "ZeroMemory(primitive" in source


def test_all_native_extractors_accept_optional_password_and_packaging_includes_bridge():
    for relative_path in (
        "scripts/windows/Extract-QdbFinancial.cs",
        "scripts/windows/Extract-QdbAccountMap.cs",
        "scripts/windows/Extract-QdbReports.cs",
        "scripts/windows/Extract-QdbVariableType.cs",
    ):
        source = read(relative_path)
        assert "PasswordStdinArgument" in source
        assert "ReadOptionalPassword" in source
        assert "QdbPassword.OpenOrThrow" in source
        assert "return 4" in source

    wrapper = read("scripts/windows/Export-QdfFinancial.ps1")
    package = read("scripts/windows/Package-QdfExport.ps1")
    module = read("QdfExport/QdfExport.psm1")
    assert "[Security.SecureString]$DatafilePassword" in wrapper
    assert "-PromptForPassword" in wrapper
    assert "ConvertTo-QdfPasswordPayload" in wrapper
    assert "--password-stdin" in wrapper
    assert "@($DatafilePassword)" not in wrapper
    assert "QdbPassword.cs'; Target" not in package
    assert "Extract-QdbFinancial.cs'; Target" not in package
    assert "Extract-QdbAccountMap.cs'; Target" not in package
    assert "Extract-QdbReports.cs'; Target" not in package
    assert "Extract-QdbVariableType.cs'; Target" not in package
    assert "QdfExportAssemblyInfo.cs'; Target" not in package
    assert "src\\qdf_tools\\qph.py" in package
    assert "[Security.SecureString]$DatafilePassword" in module
    assert "[switch]$PromptForPassword" in module
    assert "Resolve-QdfCSharpCompiler" in wrapper
    assert "Resolve-QdfCSharpCompiler" in package
    assert "Resolve-QdfExportRoot" in wrapper
    assert "Resolve-QdfExportRoot" in package
    assert "scripts\\reproduce_qdb_report.py" in package
    assert "src\\qdf_tools\\report_reproduction.py" in package
    assert "scripts\\windows\\Reproduce-QdfReport.ps1" in package
    assert "Reproduce-QdfReport" in module
    assert "Reproduce-QdfReport" in read("QdfExport/QdfExport.psd1")
    assert "Join-Path $PSScriptRoot '..\\..'" not in wrapper
    assert "Join-Path $PSScriptRoot '..\\..'" not in package
    assert "v4.0.30319" not in wrapper
    assert "v4.0.30319" not in package

    scheduled = read("scripts/windows/Invoke-QdfScheduledExport.ps1")
    config = read("scripts/windows/QdfScheduledExport.config.psd1.example")
    assert "DatafilePasswordSecretPath" in scheduled
    assert "ConvertTo-SecureString" in scheduled
    assert "DatafilePasswordSecretPath" in config
    assert "%ProgramData%\\QdfExport" in config
    assert "C:\\ProgramData\\QdfExport" not in config
    assert "ExpandEnvironmentVariables" in scheduled
    assert "$exportParameters = @{" in scheduled
    assert "$exportParameters.DatafilePassword = $datafilePassword" in scheduled
    assert "& $wrapper @exportParameters" in scheduled
    assert "$exportArguments" not in scheduled
    assert (
        "[IO.File]::Replace($temporaryDestination, $destination, [NullString]::Value)" in scheduled
    )
    assert "[IO.File]::Replace($temporaryDestination, $destination, $null)" not in scheduled


def test_scheduled_export_registration_sets_description_and_windows_10_compatibility():
    registration = read("scripts/windows/Register-QdfScheduledExport.ps1")

    assert "$description = 'Runs the daily read-only Quicken QDF export" in registration
    assert "-Compatibility Win10" in registration
    assert "-Description $description" in registration


def test_windows_export_uses_concise_named_progress_by_default():
    wrapper = read("scripts/windows/Export-QdfFinancial.ps1")

    assert "Invoke-QdfNativeStage" in wrapper
    assert "& $Executable @nativeArguments | Out-Null" in wrapper
    assert "$VerbosePreference -eq 'Continue'" in wrapper
    assert "type=0x" not in wrapper
    assert "run --quiet --script" in wrapper
    assert "[QDF export] Complete" in wrapper


def test_scheduled_export_includes_a_readable_run_summary():
    scheduled = read("scripts/windows/Invoke-QdfScheduledExport.ps1")

    assert "function Format-QdfTimestamp" in scheduled
    assert "ddd, MMM d, yyyy h:mm:ss tt zzz" in scheduled
    assert "Start time (local):" in scheduled
    assert "End time (local):" in scheduled
    assert "Elapsed:" in scheduled
    assert "Status: $runStatus" in scheduled
    assert "({1:N0} bytes)" in scheduled
    assert "$runSucceeded = $true" in scheduled
