EVE Retroindustry - Windows installer
=====================================

1. Extract this ZIP first (right-click > Extract All).
2. Run EVE_Retroindustry-*-win64-setup.exe from the extracted folder.

No administrator rights are needed. It installs for the current user only,
into %LOCALAPPDATA%\Programs\EVE Retroindustry. Your data - characters,
prices, settings - lives separately in %LOCALAPPDATA%\EVE Retroindustry and
survives both updates and uninstalls.

Windows may warn that the file is "not commonly downloaded": the app is not
code-signed yet. The SHA256 of every asset is listed on the GitHub release
page if you want to check it.


If setup fails with "Unable to execute file in the temporary directory.
Setup aborted. Error 5: Access is denied."
-------------------------------------------------------------------------
Your Windows temp folder or your security software is blocking the
installer. It is not a fault in this build - the installer never got as far
as running. In order of likelihood:

  * Antivirus or HIPS blocked it. Allow the file or add an exclusion, then
    run it again.
  * %TEMP% is unwritable or corrupt. Press Win+R, type %TEMP%, and check
    that the folder opens and that you can create a file in it. Emptying it
    usually fixes this.
  * Company policy (AppLocker / Software Restriction Policies) forbids
    running programs from %TEMP%.

Do NOT "run as administrator" to get around it, even though most guides on
the web suggest exactly that. This installer is per-user by design; started
as administrator it installs into the administrator's profile instead of
yours, and the built-in updater stops working.

The quickest way past it is the PORTABLE build: on the same release page,
EVE_Retroindustry-*-win64-portable.zip. It needs no installer - extract it
anywhere and run EVE_Retroindustry.exe. It updates itself the same way.


Links
-----
Website:  https://everetroindustry.github.io
Source:   https://github.com/EVERetroIndustry/Eve-retroindustry
Releases: https://github.com/EVERetroIndustry/Eve-retroindustry/releases
