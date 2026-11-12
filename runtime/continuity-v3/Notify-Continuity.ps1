param([Parameter(Mandatory=$true)][string]$InputPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if(-not [Environment]::UserInteractive){ throw 'Interactive Windows desktop required' }
$inputFile=(Resolve-Path -LiteralPath $InputPath).Path
$notice=Get-Content -LiteralPath $inputFile -Raw | ConvertFrom-Json
if($notice.id -notmatch '^[a-f0-9]{64}$' -or $notice.nonce -notmatch '^[a-f0-9]{32}$'){throw 'Invalid notice identity'}
if($notice.text.Length -gt 3000 -or $notice.text.Length -lt 1){throw 'Invalid notice text'}
$folder=Split-Path -LiteralPath $inputFile
$receiptPath=Join-Path $folder 'shown.json'
$form=New-Object System.Windows.Forms.Form
$form.Text='Codex continuity notice'
$form.Size=New-Object System.Drawing.Size(540,285)
$form.StartPosition='CenterScreen'
$form.FormBorderStyle='FixedDialog'
$form.MaximizeBox=$false
$form.MinimizeBox=$false
$form.TopMost=$true
$form.ShowInTaskbar=$true
$form.BackColor=[System.Drawing.Color]::FromArgb(245,247,250)
$label=New-Object System.Windows.Forms.TextBox
$label.Location=New-Object System.Drawing.Point(20,20)
$label.Size=New-Object System.Drawing.Size(485,175)
$label.Font=New-Object System.Drawing.Font('Segoe UI',11)
$label.Multiline=$true
$label.ReadOnly=$true
$label.ScrollBars='Vertical'
$label.WordWrap=$true
$label.Text=[string]$notice.text
$form.Controls.Add($label)
$button=New-Object System.Windows.Forms.Button
$button.Text='Dismiss'
$button.Location=New-Object System.Drawing.Point(405,205)
$button.Size=New-Object System.Drawing.Size(100,30)
$button.Add_Click({$form.Close()})
$form.Controls.Add($button)
$timer=New-Object System.Windows.Forms.Timer
$timer.Interval=15000
$timer.Add_Tick({$timer.Stop();$form.Close()})
$form.Add_Shown({
    $form.Refresh()
    if(-not $form.Visible -or $form.Handle -eq [IntPtr]::Zero){throw 'Notice was not shown'}
    $receipt=[ordered]@{id=[string]$notice.id;nonce=[string]$notice.nonce;status='shown';pid=$PID;
        window_handle=$form.Handle.ToInt64();shown_utc=[DateTime]::UtcNow.ToString('o');user_acknowledged=$false}
    [IO.File]::WriteAllText($receiptPath,($receipt|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    $timer.Start()
})
try{[System.Windows.Forms.Application]::Run($form)}finally{$timer.Dispose();$form.Dispose()}
