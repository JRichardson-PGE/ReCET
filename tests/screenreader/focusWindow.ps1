# Bring the visible top-level window whose title starts with $env:RECET_WINDOW_TITLE to the foreground (used by recet.nvda.spec.ts before
# NVDA looks for the browser). A tap of Alt first lets SetForegroundWindow take the foreground from a background process. Prints True / False / notfound.
$prefix = $env:RECET_WINDOW_TITLE
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class RecetWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
  public static IntPtr Find(string prefix) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate (IntPtr h, IntPtr l) {
      if (!IsWindowVisible(h)) return true;
      var sb = new StringBuilder(512); GetWindowText(h, sb, 512);
      if (sb.ToString().StartsWith(prefix)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
$h = [RecetWin]::Find($prefix)
if ($h -eq [IntPtr]::Zero) { Write-Output "notfound"; exit 0 }
[RecetWin]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[RecetWin]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[RecetWin]::ShowWindow($h, 9) | Out-Null
Write-Output ([RecetWin]::SetForegroundWindow($h))
