# NetView serial bridge for Windows (2026-10-06).
#
# Linux and IOS nodes put a shell on their serial console; Windows desktops put
# nothing there, so NetView could not test from them. This answers a FIXED set
# of network checks on COM1 - it cannot run anything else (Andrew chose this
# over a general PowerShell prompt). Every argument is validated before use.
#
#   hello                         -> "nv hello <computer> <utc epoch>"
#   ping <host> [interval_ms] [count] [timeout_ms]
#   watch <host> <interval_ms> <check_every_ms> [dns=<name>[@server]] [tcp=<host>:<port>]
#                                 -> ping stream + background service checks
#                                 -> one line per probe, until count or any input
#   tcp <host> <port> [timeout_ms]-> one timed TCP connect
#   dns <name> [server]           -> one timed lookup
#   dhcp                          -> ipconfig /release + /renew, then the lease
#   ip                            -> addresses, gateway, DNS servers, DHCP server
#   time                          -> local clock as UTC epoch
#
# Installed as a startup task (SYSTEM) by setup.ps1, so it runs with nobody
# logged in. Remove: Unregister-ScheduledTask NetViewBridge -Confirm:$false;
#                    Remove-Item C:\ProgramData\NetView -Recurse

$ErrorActionPreference = 'Continue'
$Prompt = 'NV> '
$HostRx = '^[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}$'

function Open-Port {
    while ($true) {
        try {
            $p = New-Object System.IO.Ports.SerialPort 'COM1', 115200, 'None', 8, 'One'
            $p.NewLine = "`r"; $p.ReadTimeout = -1; $p.WriteTimeout = 5000; $p.DtrEnable = $true
            $p.Open(); return $p
        } catch { Start-Sleep -Seconds 5 }
    }
}
$port = Open-Port
function Send([string]$t) { $port.Write(($t -replace "`r?`n", "`r`n")) }
function Now { '{0:F3}' -f ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0) }
function Int([string]$v, [int]$def, [int]$min, [int]$max) {
    if (-not $v) { return $def }
    $n = 0; if (-not [int]::TryParse($v, [ref]$n) -or $n -lt $min -or $n -gt $max) { throw "bad number: $v" }
    return $n
}
function HostArg([string]$v) { if ($v -notmatch $HostRx) { throw "bad host: $v" }; return $v }

function Do-Ping($a) {
    $target = HostArg $a[0]; $iv = Int $a[1] 200 50 10000; $count = Int $a[2] 0 0 100000; $to = Int $a[3] 1000 50 10000
    $pinger = New-Object System.Net.NetworkInformation.Ping
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $e0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $seq = 0
    while (($count -eq 0 -or $seq -lt $count) -and $port.BytesToRead -eq 0) {
        $wait = $seq * $iv - $sw.ElapsedMilliseconds
        if ($wait -gt 0) { Start-Sleep -Milliseconds $wait }
        $sent = '{0:F3}' -f ($e0 + $sw.ElapsedMilliseconds / 1000.0)
        try {
            $r = $pinger.Send($target, [Math]::Min($to, [Math]::Max(100, $iv - 20)))
            $l = if ($r.Status -eq 'Success') { "nvping seq=$seq sent=$sent ok rtt=$($r.RoundtripTime)" } else { "nvping seq=$seq sent=$sent lost $($r.Status)" }
        } catch { $l = "nvping seq=$seq sent=$sent lost error" }
        Send "$l`n"; $seq++
    }
    if ($port.BytesToRead -gt 0) { [void]$port.ReadExisting() }
    "nvping done sent=$seq"
}

# watch <host> <interval_ms> <check_every_ms> [dns=<name>[@<server>]]... [tcp=<host>:<port>]...
# The ping stream of Do-Ping, plus service checks run in the background every
# check_every_ms (each in its own runspace, so a slow lookup never delays a
# probe). Prints nvping / nvdns / nvtcp lines until any input arrives.
$CheckScript = {
    param($kind, $arg)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    if ($kind -eq 'dns') {
        $name, $server = $arg -split '@', 2
        try {
            $p = @{ Name = $name; DnsOnly = $true; NoHostsFile = $true; QuickTimeout = $true; ErrorAction = 'Stop' }
            if ($server) { $p.Server = $server }
            $ips = ((Resolve-DnsName @p) | Where-Object { $_.IPAddress } | ForEach-Object { $_.IPAddress }) -join ','
            "nvdns $arg ok ms=$($sw.ElapsedMilliseconds) answer=$ips"
        } catch { "nvdns $arg fail ms=$($sw.ElapsedMilliseconds)" }
    } else {
        $h, $port = $arg -split ':', 2
        $c = New-Object System.Net.Sockets.TcpClient
        try {
            $t = $c.ConnectAsync($h, [int]$port)
            if ($t.Wait(1000) -and $c.Connected) { "nvtcp $arg ok ms=$($sw.ElapsedMilliseconds)" }
            else { "nvtcp $arg fail ms=$($sw.ElapsedMilliseconds) timeout" }
        } catch { "nvtcp $arg fail ms=$($sw.ElapsedMilliseconds) refused" } finally { $c.Close() }
    }
}

function Do-Watch($a) {
    $target = HostArg $a[0]; $iv = Int $a[1] 200 50 10000; $every = Int $a[2] 1000 200 60000
    $checks = @()
    foreach ($t in @($a | Select-Object -Skip 3 | Where-Object { $_ })) {
        if ($t -match '^dns=([A-Za-z0-9.\-]+)(@[A-Za-z0-9.\-:]+)?$') {
            $checks += @{ kind = 'dns'; arg = $t.Substring(4); ps = $null; h = $null }
        } elseif ($t -match '^tcp=([A-Za-z0-9.\-]+):(\d{1,5})$') {
            $checks += @{ kind = 'tcp'; arg = $t.Substring(4); ps = $null; h = $null }
        } else { throw "bad check: $t" }
    }
    $pool = [runspacefactory]::CreateRunspacePool(1, [Math]::Max(1, $checks.Count))
    $pool.Open()
    $pinger = New-Object System.Net.NetworkInformation.Ping
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $e0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $seq = 0; $nextCheck = 0
    try {
        while ($port.BytesToRead -eq 0) {
            foreach ($c in $checks) {                       # collect finished checks
                if ($c.h -and $c.h.IsCompleted) {
                    $r = ($c.ps.EndInvoke($c.h) | Out-String).Trim()
                    $c.ps.Dispose(); $c.ps = $null; $c.h = $null
                    Send "$r`n"
                }
            }
            if ($sw.ElapsedMilliseconds -ge $nextCheck) {   # start the next round
                foreach ($c in $checks) {
                    if (-not $c.ps) {
                        $c.ps = [powershell]::Create(); $c.ps.RunspacePool = $pool
                        [void]$c.ps.AddScript($CheckScript).AddArgument($c.kind).AddArgument($c.arg)
                        $c.h = $c.ps.BeginInvoke()
                    }
                }
                $nextCheck += $every
            }
            $wait = $seq * $iv - $sw.ElapsedMilliseconds
            if ($wait -gt 0) { Start-Sleep -Milliseconds $wait }
            $sent = '{0:F3}' -f ($e0 + $sw.ElapsedMilliseconds / 1000.0)
            try {
                $r = $pinger.Send($target, [Math]::Max(100, $iv - 20))
                $l = if ($r.Status -eq 'Success') { "nvping seq=$seq sent=$sent ok rtt=$($r.RoundtripTime)" } else { "nvping seq=$seq sent=$sent lost $($r.Status)" }
            } catch { $l = "nvping seq=$seq sent=$sent lost error" }
            Send "$l`n"; $seq++
        }
    } finally {
        foreach ($c in $checks) { if ($c.ps) { $c.ps.Dispose() } }
        $pool.Close()
    }
    [void]$port.ReadExisting()
    "nvwatch done sent=$seq"
}

function Do-Tcp($a) {
    $h = HostArg $a[0]; $p = Int $a[1] 0 1 65535; $to = Int $a[2] 1000 50 10000
    $c = New-Object System.Net.Sockets.TcpClient; $sw = [Diagnostics.Stopwatch]::StartNew(); $at = Now
    try {
        $t = $c.ConnectAsync($h, $p)
        if ($t.Wait($to) -and $c.Connected) { "nvtcp ${h}:$p at=$at ok ms=$($sw.ElapsedMilliseconds)" }
        else { "nvtcp ${h}:$p at=$at fail ms=$($sw.ElapsedMilliseconds) timeout" }
    } catch { "nvtcp ${h}:$p at=$at fail ms=$($sw.ElapsedMilliseconds) refused" } finally { $c.Close() }
}

function Do-Dns($a) {
    $n = HostArg $a[0]; $s = if ($a[1]) { HostArg $a[1] } else { '' }
    $sw = [Diagnostics.Stopwatch]::StartNew(); $at = Now
    try {
        $p = @{ Name = $n; DnsOnly = $true; NoHostsFile = $true; QuickTimeout = $true; ErrorAction = 'Stop' }
        if ($s) { $p.Server = $s }
        $ips = ((Resolve-DnsName @p) | Where-Object { $_.IPAddress } | ForEach-Object { $_.IPAddress }) -join ','
        "nvdns $n at=$at ok ms=$($sw.ElapsedMilliseconds) answer=$ips server=$s"
    } catch { "nvdns $n at=$at fail ms=$($sw.ElapsedMilliseconds) server=$s" }
}

function Do-Ip {
    Get-NetIPConfiguration | Where-Object { $_.IPv4Address } | ForEach-Object {
        $i = $_.InterfaceIndex
        $d = Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "InterfaceIndex=$i"
        "nvip if=$($_.InterfaceAlias) addr=$($_.IPv4Address.IPAddress -join ',') gw=$($_.IPv4DefaultGateway.NextHop -join ',') " +
        "dns=$($_.DNSServer.ServerAddresses -join ',') dhcp=$($d.DHCPEnabled) dhcpserver=$($d.DHCPServer) " +
        "obtained=$($d.DHCPLeaseObtained) expires=$($d.DHCPLeaseExpires)"
    }
}

function Do-Dhcp {
    $at = Now; $sw = [Diagnostics.Stopwatch]::StartNew()
    ipconfig /release | Out-Null
    $rel = Now
    ipconfig /renew | Out-Null
    "nvdhcp at=$at released=$rel renewed=$(Now) ms=$($sw.ElapsedMilliseconds)"
    Do-Ip
}

Send "`nNetView bridge ready on $env:COMPUTERNAME`n$Prompt"
while ($true) {
    try { $line = $port.ReadLine() } catch { try { $port.Close() } catch {}; $port = Open-Port; continue }
    $parts = @($line.Trim([char]10, [char]13, ' ', [char]0) -split '\s+' | Where-Object { $_ })
    if (-not $parts) { Send "`n$Prompt"; continue }
    $cmd = $parts[0].ToLower(); $a = @($parts | Select-Object -Skip 1) + @($null, $null, $null, $null)
    $out = try {
        switch ($cmd) {
            'hello' { "nv hello $env:COMPUTERNAME $(Now)" }
            'ping'  { Do-Ping $a }
            'watch' { Do-Watch $a }
            'tcp'   { Do-Tcp $a }
            'dns'   { Do-Dns $a }
            'dhcp'  { Do-Dhcp }
            'ip'    { Do-Ip }
            'time'  { "nvtime $(Now)" }
            default { "nv error unknown command '$cmd' (hello ping watch tcp dns dhcp ip time)" }
        }
    } catch { "nv error $($_.Exception.Message)" }
    Send ("`n" + (($out | Out-String -Width 250).TrimEnd()) + "`n$Prompt")
}
