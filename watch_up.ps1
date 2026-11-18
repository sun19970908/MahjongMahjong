# 2 号的自动手看守：watch.py 挂了就立刻拉起来。
$ErrorActionPreference = "Continue"
Set-Location "d:\Users\C\Documents\Empty\Mahjong"
$python = "D:\Program Files\Python313\python.exe"
while ($true) {
    $stamp = Get-Date -Format "HH:mm:ss"
    Add-Content -Path "watch.out" -Value "$stamp [看守] 拉起 watch.py"
    & $python watch.py --seat 2 --brain mine --hold 30 *>> watch.out
    Add-Content -Path "watch.out" -Value "$(Get-Date -Format 'HH:mm:ss') [看守] watch.py 退出（$LASTEXITCODE），2 秒后重来"
    Start-Sleep -Seconds 2
}
