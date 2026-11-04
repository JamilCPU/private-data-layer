<#
.SYNOPSIS
  Trust the lab CA on this Windows machine for browsers, curl and Claude Code.

.DESCRIPTION
  Imports the lab CA certificate into the current user's Trusted Root store (browsers and most
  tools) and sets NODE_EXTRA_CA_CERTS for the current user (Node-based tools such as Claude Code
  ignore the Windows store). Restart Claude Code afterwards.

  Name resolution is separate: *.lab names resolve only through the lab DNS. Either point this
  machine's DNS at the lab resolvers (preferred) or add hosts-file entries for the ingress hosts.

.PARAMETER CaFile
  Path to lab-ca.pem (the CA used by the cluster's cert-manager ClusterIssuer).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\trust-lab-ca.ps1 -CaFile "$env:LOCALAPPDATA\private-data-layer\lab-ca.pem"
#>
param(
    [Parameter(Mandatory = $true)] [string] $CaFile
)
$ErrorActionPreference = "Stop"
$CaFile = (Resolve-Path $CaFile).Path
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $CaFile
Write-Host "CA: $($cert.Subject)  thumbprint $($cert.Thumbprint)"

$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
$store.Open("ReadWrite")
if ($store.Certificates.Find("FindByThumbprint", $cert.Thumbprint, $false).Count -eq 0) {
    $store.Add($cert)
    Write-Host "Imported into CurrentUser\Root."
} else {
    Write-Host "Already trusted."
}
$store.Close()

[Environment]::SetEnvironmentVariable("NODE_EXTRA_CA_CERTS", $CaFile, "User")
Write-Host "NODE_EXTRA_CA_CERTS set for the current user: $CaFile (restart Claude Code)."
Write-Host "Reminder: *.lab names need the lab DNS (or hosts-file entries) on this machine."
