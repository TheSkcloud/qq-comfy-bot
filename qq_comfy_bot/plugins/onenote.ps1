$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $app = New-Object -ComObject OneNote.Application
    $xml = ''
    $app.GetHierarchy('', 4, [ref]$xml)
    [xml]$tree = $xml
    $pages = @($tree.SelectNodes("//*[local-name()='Page']") | Where-Object { $_.isInRecycleBin -ne 'true' } | Sort-Object lastModifiedTime -Descending)
    $all = @($pages | ForEach-Object { @{id=$_.ID;title=$_.name;modified=$_.lastModifiedTime;section=$_.ParentNode.name} })
    if ($request.action -eq 'list') {
        @{pages=@($all | Select-Object -First 12);total=$all.Count;source='OneNote desktop synchronized data'} | ConvertTo-Json -Depth 7 -Compress
        exit 0
    }
    $query = [string]$request.query
    if (-not $query.Trim() -and $request.action -ne 'profile') { throw 'Please supply a page title or search term.' }
    $titleMatches = @($pages | Where-Object { $_.name.IndexOf($query, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
    $selected = if ($request.action -eq 'profile') { @($pages | Where-Object { ($_.ParentNode.name -match '生活|随笔|日记|记在|角落') -or ($_.name -match '日记|随笔|生活') } | Select-Object -First 3) } elseif ($titleMatches.Count -gt 0) { @($titleMatches | Select-Object -First 5) } else { @($pages | Select-Object -First 10) }
    $results = @()
    foreach ($page in $selected) {
        $content = ''
        $app.GetPageContent($page.ID, [ref]$content, 0)
        [xml]$doc = $content
        $parts = @($doc.SelectNodes("//*[local-name()='T']") | ForEach-Object { [System.Net.WebUtility]::HtmlDecode(($_.InnerText -replace '<[^>]+>', ' ')) })
        $text = $parts -join "`n"
        if ($request.action -eq 'profile' -or $titleMatches.Count -gt 0 -or $text.IndexOf($query, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $limit = if($request.action -eq 'profile'){1400}else{5000}
            $results += @{id=$page.ID;title=$page.name;modified=$page.lastModifiedTime;text=$text.Substring(0,[Math]::Min($limit,$text.Length));truncated=($text.Length -gt $limit)}
        }
        if ($results.Count -ge 3) { break }
    }
    @{pages=$results;scanned=$selected.Count;total=$all.Count;scope= $(if($titleMatches.Count -gt 0){'title matches'}else{'latest ten pages only'});source='OneNote desktop synchronized data'} | ConvertTo-Json -Depth 8 -Compress
} catch {
    @{error=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}
