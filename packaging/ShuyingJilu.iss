; 书影记录 Windows 安装包（Inno Setup 6）
; 由 packaging\build_windows.bat 调用：
;   ISCC.exe /DMyAppVersion=0.1.1 packaging\ShuyingJilu.iss
; 编译前需先执行 PyInstaller，确保 dist\ShuyingJilu\ShuyingJilu.exe 已生成。

#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif

#define MyAppName "书影记录"
#define MyAppExeName "ShuyingJilu.exe"
#define MyAppPublisher "书影记录"

[Setup]
; AppId 唯一标识本应用，后续版本升级必须保持不变。
AppId={{8F5C2B1E-4A7D-4E3B-9C61-2D7A5E9F3B84}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 应用数据写入 %APPDATA%，安装到当前用户目录即可，无需管理员权限。
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
; 产物统一命名：ShuyingJilu-<版本>-windows-x64-setup.exe（纯 ASCII）。
OutputBaseFilename=ShuyingJilu-{#MyAppVersion}-windows-x64-setup
SetupIconFile=icon.ico
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Chinese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\ShuyingJilu\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\ShuyingJilu\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
