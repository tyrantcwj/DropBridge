# Validate the exact INF bytes and the CAT signer without installing anything.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$DriverDirectory
)

$ErrorActionPreference = 'Stop'
$inf = Join-Path $DriverDirectory 'airbridge-winusb.inf'
$cat = Join-Path $DriverDirectory 'airbridge-winusb.cat'
$cer = Join-Path $DriverDirectory 'dropbridge-driver.cer'
foreach ($path in $inf, $cat, $cer) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "missing driver file: $path" }
}
$cat = (Resolve-Path -LiteralPath $cat).ProviderPath
$cer = (Resolve-Path -LiteralPath $cer).ProviderPath

# Check the signature itself and pin it to the certificate shipped beside it.
# The release certificate is self-signed; no trust store changes are needed.
Add-Type -AssemblyName System.Security
$cms = New-Object System.Security.Cryptography.Pkcs.SignedCms
$cms.Decode([IO.File]::ReadAllBytes($cat))
$cms.CheckSignature($true)
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($cer)
try {
    $signers = @($cms.SignerInfos | ForEach-Object { $_.Certificate.Thumbprint })
    if ($signers -notcontains $cert.Thumbprint) {
        throw "CAT signer does not match the bundled certificate: $cer"
    }
} finally {
    $cert.Dispose()
}

# The current MakeCat package uses SHA-1 member tags; also accept SHA-256.
# Query the catalog table through Win32 rather than searching the CAT bytes.
if (-not ('DropBridge.DriverCatalog' -as [type])) {
    Add-Type -Namespace DropBridge -Name DriverCatalog -MemberDefinition @'
[DllImport("wintrust.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
public static extern IntPtr CryptCATOpen(string path, uint flags, IntPtr provider,
                                         uint publicVersion, uint encoding);
[DllImport("wintrust.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
public static extern IntPtr CryptCATGetMemberInfo(IntPtr catalog, string hash);
[DllImport("wintrust.dll", ExactSpelling = true)]
public static extern bool CryptCATClose(IntPtr catalog);
'@
}
$handle = [DropBridge.DriverCatalog]::CryptCATOpen($cat, 4, [IntPtr]::Zero, 0, 0)
if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr](-1)) {
    throw "cannot open driver catalog: $cat"
}
try {
    foreach ($algorithm in 'SHA256', 'SHA1') {
        $hash = (Get-FileHash -LiteralPath $inf -Algorithm $algorithm).Hash
        if ([DropBridge.DriverCatalog]::CryptCATGetMemberInfo($handle, $hash) -ne [IntPtr]::Zero) {
            Write-Host "driver package verified: $algorithm $hash"
            return
        }
    }
    throw "INF hash missing from catalog: $inf. Restore the exact signed INF bytes (including line endings) or rebuild the CAT."
} finally {
    [void][DropBridge.DriverCatalog]::CryptCATClose($handle)
}
