# 바로가기: 바탕화면과 시작 메뉴에 NewsRadar(오늘 정리 보기), NewsRadar 설정(설정 화면 다시 열기)
# 시작 메뉴의 NewsRadar 바로가기에는 앱 ID를 달아, 알림 보낸 쪽 이름이 'NewsRadar'로 보이게 한다 (notify.ps1)
# 지우기: shortcuts.ps1 -Remove
param([switch]$Remove)
$appId = 'NewsRadar'
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')   # 시작 메뉴 > 프로그램
$icon = Join-Path (Split-Path -Parent $PSScriptRoot) 'icon.ico'
$links = @(
    @{ Name = 'NewsRadar.lnk';      File = 'open.ps1';  Desc = '오늘의 정리 보기'; Id = $appId }
    @{ Name = 'NewsRadar 설정.lnk'; File = 'setup.ps1'; Desc = '분야·받을 시각 바꾸기, Claude 다시 연결'; Id = $null }
)

if ($Remove) {
    foreach ($l in $links) { foreach ($dir in $desktop, $programs) { Remove-Item (Join-Path $dir $l.Name) -ErrorAction SilentlyContinue } }
    return
}

# 바로가기에 앱 ID(System.AppUserModel.ID)를 다는 코드. Windows 기본 기능(shell32)만 쓴다
if (-not ('NewsRadarAumid' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class NewsRadarAumid {
    [StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PropertyKey { public Guid FormatId; public uint PropertyId; }
    [StructLayout(LayoutKind.Explicit)] public struct PropVariant { [FieldOffset(0)] public ushort VarType; [FieldOffset(8)] public IntPtr Pointer; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint count); void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value); void SetValue(ref PropertyKey key, ref PropVariant value); void Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid, out IPropertyStore store);
    public static void Set(string shortcut, string id) {
        Guid iid = typeof(IPropertyStore).GUID;
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(shortcut, IntPtr.Zero, 2 /* 읽기·쓰기 */, ref iid, out store);
        PropertyKey key = new PropertyKey { FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), PropertyId = 5 };
        PropVariant value = new PropVariant { VarType = 31 /* 문자열 */, Pointer = Marshal.StringToCoTaskMemUni(id) };
        try { store.SetValue(ref key, ref value); store.Commit(); }
        finally { Marshal.FreeCoTaskMem(value.Pointer); Marshal.ReleaseComObject(store); }
    }
}
'@
}

$shell = New-Object -ComObject WScript.Shell
foreach ($l in $links) {
    foreach ($dir in $desktop, $programs) {
        $path = Join-Path $dir $l.Name
        $s = $shell.CreateShortcut($path)
        $s.TargetPath = 'powershell.exe'
        $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot $l.File)`""
        $s.WindowStyle = 7   # 최소화된 창
        $s.Description = $l.Desc
        $s.IconLocation = if (Test-Path $icon) { "$icon,0" } else { "$env:SystemRoot\System32\shell32.dll,13" }
        $s.Save()
        if ($l.Id -and $dir -eq $programs) {
            try { [NewsRadarAumid]::Set($path, $l.Id) } catch { }   # 안 되면 알림 이름만 'Windows PowerShell'로 보인다
        }
    }
}
