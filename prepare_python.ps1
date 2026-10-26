# Download a portable Python (no admin rights needed) into .\runtime
# Used by 启动源码.bat when no system Python is found.
$ErrorActionPreference = "Stop"

$V = "3.12.10"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dir = Join-Path $Root "runtime"
$Py = Join-Path $Dir "python.exe"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not (Test-Path $Py)) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $Zip = Join-Path $env:TEMP "python-$V-embed-amd64.zip"
    $Url = "https://www.python.org/ftp/python/$V/python-$V-embed-amd64.zip"
    if (-not (Test-Path $Zip)) {
        Write-Host "Downloading portable Python $V ..."
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
    }
    Write-Host "Extracting to $Dir ..."
    Expand-Archive -Path $Zip -DestinationPath $Dir -Force
}

# embeddable python ignores site-packages unless "import site" is enabled
$Pth = Join-Path $Dir ("python" + ($V.Split(".")[0] + $V.Split(".")[1]) + "._pth")
if (Test-Path $Pth) {
    $c = Get-Content $Pth
    $c = $c -replace "^#\s*import site", "import site"
    Set-Content -Path $Pth -Value $c -Encoding ASCII
}

# pip
& $Py -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pip ..."
    $GetPip = Join-Path $env:TEMP "get-pip.py"
    if (-not (Test-Path $GetPip)) {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip -UseBasicParsing
    }
    & $Py $GetPip --no-warn-script-location
}

Write-Host "PORTABLE_PYTHON_OK"
