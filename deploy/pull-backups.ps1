<#
把服务器上的备份**拉到本机**的那个目录（默认 `D:\document\DeepGrill-back`）。

    用法（先手动跑一次确认能通）：
        pwsh -File deploy\pull-backups.ps1 -Server deepgrill@203.0.113.7
        pwsh -File deploy\pull-backups.ps1 -Server deepgrill@203.0.113.7 -Dest D:\document\DeepGrill-back

    挂到任务计划程序（每天 04:30，服务器 03:30 备份之后）：
        schtasks /create /tn "DeepGrill 拉备份" /sc daily /st 04:30 ^
            /tr "pwsh -NoProfile -File D:\document\DeepGrill-next\deploy\pull-backups.ps1 -Server deepgrill@203.0.113.7"

## 为什么是"本机拉"而不是"服务器推"

家用宽带上的机器通常没有被公网可达的地址（NAT 后面），服务器推不过来；而本机连
服务器是出站连接，一定通。方向反过来之后，"备份到底有没有到达异地"这个问题的答案
就不再依赖家里的网络拓扑。

## 它怎么保证"真的到了"

① 只拉 `deepgrill-*.db.gz` 与它们的 `.json` 清单；
② 复制完**本机再核一遍 sha256**（清单里有），不一致就删掉那个文件并非零退出 ——
   "拉了一半的备份"比没有备份更危险（它会让人以为有）；
③ 拉完按份数保留最近 `-Keep` 份（本机磁盘也是有限的）。

## 退出码

0 = 这一轮拉成功且校验通过；非 0 = 有问题（任务计划程序里会记成失败那一档）。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,          # 例如 deepgrill@203.0.113.7
    [string]$Dest = "D:\document\DeepGrill-back",
    [string]$Remote = "/srv/deepgrill/backups",
    [int]$Keep = 14,
    [string]$SshKey = ""                                     # 可选：-i 指定的私钥
)

$ErrorActionPreference = "Stop"
$sshArgs = @()
if ($SshKey) { $sshArgs += @("-i", $SshKey) }

if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
Write-Host "从 $Server`:$Remote 拉到 $Dest"

# ① 远端有哪些备份
$listing = & ssh @sshArgs $Server "ls -1 $Remote/deepgrill-*.db.gz 2>/dev/null"
if ($LASTEXITCODE -ne 0) { Write-Error "ssh 连不上或列目录失败（退出码 $LASTEXITCODE）"; exit 1 }
$names = @($listing | Where-Object { $_ -and $_.Trim() } | ForEach-Object { Split-Path $_ -Leaf })
if ($names.Count -eq 0) { Write-Error "远端 $Remote 里没有备份文件"; exit 1 }

# ② 只拉本机还没有的那些（已存在的按大小相同跳过，避免每天重传 15MB）
$todo = @()
foreach ($name in $names) {
    $local = Join-Path $Dest $name
    if (-not (Test-Path $local)) { $todo += $name; continue }
    $remoteSize = & ssh @sshArgs $Server "stat -c %s $Remote/$name"
    if ([int64]$remoteSize -ne (Get-Item $local).Length) { $todo += $name }
}
if ($todo.Count -eq 0) { Write-Host "没有新备份（$($names.Count) 份都在）"; exit 0 }
Write-Host "要拉 $($todo.Count) 份：$($todo -join ', ')"

foreach ($name in $todo) {
    $local = Join-Path $Dest $name
    & scp @sshArgs "${Server}:$Remote/$name" $local
    if ($LASTEXITCODE -ne 0) { Write-Error "scp 失败：$name"; exit 1 }
    & scp @sshArgs "${Server}:$Remote/$name.json" "$local.json"
    if ($LASTEXITCODE -ne 0) { Write-Error "scp 失败：$name.json"; exit 1 }

    # ③ 本机核校验和（清单里有）
    $manifest = Get-Content "$local.json" -Raw | ConvertFrom-Json
    $actual = (Get-FileHash -Path $local -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $manifest.sha256) {
        Remove-Item $local, "$local.json" -Force
        Write-Error "校验和不一致（拉下来的坏了，已删）：$name"
        exit 1
    }
    Write-Host "  [ok] $name（$([math]::Round((Get-Item $local).Length / 1MB, 1)) MB，校验和一致）"
}

# ④ 本机也保留最近 N 份
$all = Get-ChildItem -Path $Dest -Filter "deepgrill-*.db.gz" | Sort-Object Name
if ($all.Count -gt $Keep) {
    $all | Select-Object -First ($all.Count - $Keep) | ForEach-Object {
        Remove-Item $_.FullName, "$($_.FullName).json" -Force -ErrorAction SilentlyContinue
        Write-Host "  按保留策略删掉 $($_.Name)"
    }
}

Write-Host "完成：$Dest 现在有 $((Get-ChildItem -Path $Dest -Filter 'deepgrill-*.db.gz').Count) 份备份"
exit 0
