# Install subtitle-tool on Windows.
#   .\install.ps1                    (from a clone)
#   powershell -ExecutionPolicy ByPass -c "irm <raw url>/install.ps1 | iex"
$ErrorActionPreference = "Stop"
$Repo = "git+https://github.com/aaro-cmd/subtitle-tool"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg is needed to sync subtitles to the audio."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "Install it with winget now? [y/N]"
        if ($answer -match '^[yY]$') { winget install --id Gyan.FFmpeg -e }
    } else {
        Write-Host "Please install ffmpeg: https://ffmpeg.org/download.html"
    }
}

$Src = $Repo
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) { $Src = $PSScriptRoot }

Write-Host "Installing subtitle-tool from $Src..."
uv tool install --force "subtitle-tool[gui,auto] @ $Src"
uv tool update-shell | Out-Null

Write-Host @"

Done. Open a new terminal, then set your API keys once:

  setx OPENSUBTITLES_API_KEY "..."   # https://www.opensubtitles.com/consumers
  setx DEEPL_API_KEY "..."           # https://www.deepl.com/pro-api (free plan)

Try it:  subtitle-tool "C:\path\to\Movie.mkv" --translate --backend deepl
"@
