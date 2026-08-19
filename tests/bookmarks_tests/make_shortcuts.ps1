$desktop = [Environment]::GetFolderPath("Desktop")
$wshell = New-Object -ComObject WScript.Shell

$targets = @(
    @("YDM_test_A_sort_fix.lnk", "D:\Program_files\YaDiskManager\bookmarks_tests\A_sort_fix_only\Запуск.bat", "Гипотеза A: только deferred sort fix"),
    @("YDM_test_B_search_clear.lnk", "D:\Program_files\YaDiskManager\bookmarks_tests\B_search_clear_all\Запуск.bat", "Гипотеза B: очистка поиска во всех путях"),
    @("YDM_test_C_logging.lnk", "D:\Program_files\YaDiskManager\bookmarks_tests\C_logging\Запуск.bat", "Гипотеза C: логирование F2 и загрузки")
)

foreach ($t in $targets) {
    $lnk = $wshell.CreateShortcut([System.IO.Path]::Combine($desktop, $t[0]))
    $lnk.TargetPath = $t[1]
    $lnk.Description = $t[2]
    $lnk.WorkingDirectory = [System.IO.Path]::GetDirectoryName($t[1])
    $lnk.Save()
    Write-Host "Created: $($t[0])"
}
