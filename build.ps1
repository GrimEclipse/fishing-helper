$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
py -3.9 -m PyInstaller --noconfirm --clean --onefile --windowed --name FishingHelper --add-data 'addon;addon' app.py
