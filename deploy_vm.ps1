param(
    [string]$HostIP,
    [string]$TailscaleHost,
    [string]$User
)

# Host details are machine-specific and stay out of the repository.
# Precedence: parameter > environment variable > .env.deploy (gitignored).
$envFile = Join-Path $PSScriptRoot ".env.deploy"
$fileVals = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$' } | ForEach-Object {
        $fileVals[$Matches[1]] = $Matches[2]
    }
}
function Resolve-Setting([string]$Value, [string]$Name) {
    if ($Value) { return $Value }
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return $fromEnv }
    return $fileVals[$Name]
}

$HostIP        = Resolve-Setting $HostIP        "LOCALPULSE_LAN_HOST"
$TailscaleHost = Resolve-Setting $TailscaleHost "LOCALPULSE_TAILSCALE_HOST"
$User          = Resolve-Setting $User          "LOCALPULSE_SSH_USER"

if (-not $HostIP -or -not $User) {
    Write-Error "Deployment target not configured. Copy .env.deploy.example to .env.deploy and fill it in."
    exit 1
}

Write-Host "Checking connectivity to $HostIP..."
$lanOk = Test-Connection -ComputerName $HostIP -Count 1 -Quiet
if (-not $lanOk) {
    if (-not $TailscaleHost) {
        Write-Error "Host $HostIP is not responding on LAN and no Tailscale host is configured."
        exit 1
    }
    Write-Warning "Host $HostIP is not responding on LAN. Checking Tailscale ($TailscaleHost)..."
    $tsOk = Test-Connection -ComputerName $TailscaleHost -Count 1 -Quiet
    if ($tsOk) {
        $HostIP = $TailscaleHost
        Write-Host "Connected via Tailscale: $HostIP"
    } else {
        Write-Error "Neither LAN ($HostIP) nor Tailscale ($TailscaleHost) is reachable. Please ensure the TrueNAS host / VM 101 is powered on."
        exit 1
    }
}

Write-Host "Syncing app and static to $HostIP..."
scp -o BatchMode=yes -o ConnectTimeout=10 -r app static "${User}@${HostIP}:~/fitness-tracker/"
if ($LASTEXITCODE -ne 0) {
    Write-Error "SCP failed."
    exit $LASTEXITCODE
}

Write-Host "Setting file permissions on $HostIP..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "${User}@${HostIP}" "chmod -R a+rX ~/fitness-tracker/app ~/fitness-tracker/static"

Write-Host "Rebuilding and restarting container on $HostIP..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "${User}@${HostIP}" "cd ~/fitness-tracker && docker compose build tracker && docker compose up -d tracker"

Write-Host "Deployment complete! Checking health..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "${User}@${HostIP}" "curl -k -s https://localhost/api/health"
