# Files the installer carries but the repository does not

`MicrosoftEdgeWebview2Setup.exe` is Microsoft's Evergreen bootstrapper for the WebView2 runtime. The
installer (`packaging/nsis/rarus.nsi`) runs it silently when the runtime is missing on the machine.

Fetch it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 and put it in this folder. The file
used for the first build was 1 867 984 bytes, version 1.3.291.9, signed by Microsoft Corporation
(SHA-256 `c90f9cdcec5e1997e581127dd1cb4526c239551908e9adbfe63ad14671bdcc94`); Microsoft updates the file
at that address, so the hash will change. `packaging/scripts/build.ps1` refuses a copy without a valid
Microsoft signature and copies the file into the build as `redist\MicrosoftEdgeWebview2Setup.exe`.
