$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
Push-Location $ProjectDir

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath;$env:Path"
}

function Install-Uv {
    if (Test-CommandExists "uv") {
        Write-Host "uv already installed: $(uv --version)"
        return
    }
    Write-Host "uv not found; installing it..."
    if (Test-CommandExists "winget") {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    }
    elseif (Test-CommandExists "choco") {
        choco install uv -y
    }
    elseif (Test-CommandExists "cargo") {
        cargo install --locked uv
    }
    else {
        $installer = Join-Path $env:TEMP "uv-install.ps1"
        Invoke-WebRequest "https://astral.sh/uv/install.ps1" -OutFile $installer
        & $installer
        Remove-Item $installer -ErrorAction SilentlyContinue
    }
    Update-ProcessPath
    if (-not (Test-CommandExists "uv")) {
        throw "uv was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

function Install-Python {
    $found = uv python find 3.12 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) {
        Write-Host "Python 3.12 available: $found"
    } else {
        Write-Host "Python 3.12 not found; installing it via uv..."
        uv python install 3.12
    }
    $venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating virtual environment in .venv..."
        uv venv --python 3.12 .venv
    }
    uv pip install --python $venvPython -e .
    Update-ProcessPath
    if (-not (Test-CommandExists "python")) {
        throw "Python was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

function Install-App {
    Write-Host "Installing agentic command to PATH via uv tool (editable)..."
    uv tool install --python 3.12 --editable --force .
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed." }
    uv tool update-shell
    $toolBin = uv tool dir --bin
    Update-ProcessPath
    $env:Path = "$toolBin;$env:Path"
    if (-not (Test-CommandExists "agentic")) {
        Write-Host "WARNING: agentic was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
        return
    }
    Write-Host "agentic ready: $((Get-Command agentic).Source)"
}

function Install-Clang {
    $compiler = @("cc", "clang", "gcc") | Where-Object { Test-CommandExists $_ } | Select-Object -First 1
    if ($compiler) {
        Write-Host "C compiler already installed."
        return
    }
    Write-Host "C compiler not found; installing LLVM/Clang..."
    if (Test-CommandExists "winget") {
        winget install --id LLVM.LLVM -e --accept-source-agreements --accept-package-agreements
    }
    elseif (Test-CommandExists "choco") {
        choco install llvm -y
    }
    else {
        Write-Host "WARNING: no winget or Chocolatey found; install LLVM/Clang or GCC manually."
        return
    }
    Update-ProcessPath
    if (-not (Test-CommandExists "clang")) {
        Write-Host "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

function Update-NvmEnv {
    # nvm-windows sets NVM_HOME/NVM_SYMLINK and puts %NVM_HOME%;%NVM_SYMLINK% on Path; load them into this process
    foreach ($name in @("NVM_HOME", "NVM_SYMLINK")) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if (-not $value) { $value = [Environment]::GetEnvironmentVariable($name, "Machine") }
        if ($value) { Set-Item "env:$name" $value }
    }
    Update-ProcessPath
    if ($env:NVM_HOME)    { $env:Path = "$env:NVM_HOME;$env:Path" }
    if ($env:NVM_SYMLINK) { $env:Path = "$env:NVM_SYMLINK;$env:Path" }
}

function Install-Node {
    if (Test-CommandExists "nvm") {
        Write-Host "nvm already installed: $(nvm version)"
    }
    else {
        Write-Host "nvm not found; installing nvm-windows..."
        if (Test-CommandExists "winget") {
            winget install --id CoreyButler.NVMforWindows -e --accept-source-agreements --accept-package-agreements
        }
        elseif (Test-CommandExists "choco") {
            choco install nvm -y
        }
        else {
            throw "no winget or Chocolatey found; install nvm-windows manually (https://github.com/coreybutler/nvm-windows)."
        }
        Update-NvmEnv
        if (-not (Test-CommandExists "nvm")) {
            throw "nvm was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
        }
    }

    Write-Host "Installing Node.js LTS via nvm..."
    nvm install lts
    if ($LASTEXITCODE -ne 0) { throw "nvm install lts failed." }
    # nvm use switches a symlink and may prompt for elevation (UAC)
    nvm use lts
    if ($LASTEXITCODE -ne 0) { throw "nvm use lts failed." }
    Update-NvmEnv
    if (-not (Test-CommandExists "node")) {
        throw "Node.js was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
    }
    Write-Host "Node.js ready: $(node --version)"
}

try {
    Install-Uv
    Install-Python
    Install-App
    Install-Clang
    Install-Node
    Write-Host "Setup complete."
    Write-Host "Activate the environment with: .venv\Scripts\Activate.ps1"
    Write-Host "Run the app from anywhere with: agentic"
}
catch {
    Write-Error "Setup failed: $_"
    exit 1
}
finally {
    Pop-Location
}
