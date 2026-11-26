# Read what the virtual Xbox 360 pad (slot 0) currently reports.
#   powershell -NoProfile -File read-pad.ps1
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential)]
public struct GP {
    public ushort wButtons;
    public byte bLeftTrigger;
    public byte bRightTrigger;
    public short sThumbLX;
    public short sThumbLY;
    public short sThumbRX;
    public short sThumbRY;
}
[StructLayout(LayoutKind.Sequential)]
public struct ST {
    public uint dwPacketNumber;
    public GP Gamepad;
}
public class XI {
    [DllImport("xinput1_4.dll")]
    public static extern int XInputGetState(int dwUserIndex, ref ST pState);
}
'@

$st = New-Object ST
$result = [XI]::XInputGetState(0, [ref]$st)
if ($result -ne 0) {
    "slot 0: not connected (error $result)"
    exit 1
}
"pad0 buttons=0x{0:X4} LX={1} LY={2} LT={3} RT={4}" -f $st.Gamepad.wButtons, $st.Gamepad.sThumbLX, $st.Gamepad.sThumbLY, $st.Gamepad.bLeftTrigger, $st.Gamepad.bRightTrigger
