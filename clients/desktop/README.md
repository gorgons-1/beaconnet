# BeaconNet Desktop

**Download:** [Windows](https://github.com/gorgons-1/scotland-bbs/releases/latest/download/BeaconNet-Windows.zip) ·
[macOS](https://github.com/gorgons-1/scotland-bbs/releases/latest/download/BeaconNet-macOS.zip) · [Linux](https://github.com/gorgons-1/scotland-bbs/releases/latest/download/BeaconNet-Linux-x86_64.tar.gz)
(always the newest release; no account needed)

A small app for your own computer (Windows, macOS or Linux). It sits in the background,
keeps an encrypted connection to your BeaconNet node, and **pops up a notification**
when something arrives:

* new bulletins, with EMERGENCY and priority traffic marked
* new private mail
* new registrations waiting for approval (sysops only)

It also gives you a page in your browser, at `http://127.0.0.1:8765`, where you can read
the boards, post, read and send mail, and, if you're a sysop on the node's local network,
approve or reject registrations. You don't need an SSH window.

## Install

Download the file for your computer from the
[latest release](https://github.com/gorgons-1/scotland-bbs/releases/latest):

| System | Download | Start |
|---|---|---|
| Windows 10/11 | `BeaconNet-Windows.zip` | unzip, double-click **BeaconNet.exe** |
| macOS 12+ (Apple silicon) | `BeaconNet-macOS.zip` | unzip, drag **BeaconNet** to Applications, open it |
| Linux (x86-64) | `BeaconNet-Linux-x86_64.tar.gz` | unpack, double-click **BeaconNet** (or run `./BeaconNet`) |

There's nothing else to install. A window opens at the login screen, and a small beacon
icon appears in the system tray (Windows), the menu bar (macOS) or the panel (Linux).
Closing the window keeps the notifications running; use the icon's **Open** or **Quit**.

**The first time only**, your computer warns you, because the app isn't signed with a
paid certificate:

* **Windows** "Windows protected your PC": click **More info**, then **Run anyway**.
* **macOS** "can't be opened": right-click (or Control-click) the app, choose **Open**,
  then **Open** again. On newer macOS: System Settings > Privacy & Security > **Open Anyway**.

The window uses Edge or Chrome's app mode if you have one (every Windows PC has Edge).
Otherwise the page opens in your normal browser, at `http://127.0.0.1:8765`.

### Running from source instead

If you'd rather not run a downloaded program, or you're on another platform (Raspberry
Pi, Intel Mac), install Python 3.9+ and run the launcher in this folder:
`BeaconNet-Windows.cmd`, `BeaconNet-macOS.command` or `beaconnet-linux.sh`. They install
the libraries the app needs (`asyncssh`, and `pystray`/`pillow` for the tray icon) for
your user only.

## First connection

On the **Settings** tab enter:

* **Node address**: the IP or name of your BBS node, e.g. `192.168.1.50`
* **Port**: `2222`
* your **BBS username** and **password**

Leave **Remember this computer** ticked. The app then makes its own key for this
computer and adds it to your account, so it never needs the password again. You can see
(and revoke) that key in the BBS under *My Account & Keys*. The password itself is never
stored.

Your account needs a password for that first login. If you registered with a key, log
in to the BBS once and set one under *My Account & Keys > P*.

## Sysops

The **Sysop** tab and registration alerts appear only for administrator accounts, and
only when the app connects from the node's local network (the same rule as the BBS's
Sysop menu). Ordinary users never see them, and the node refuses admin requests from
anyone else.

## Start it with your computer

* **Windows**: press Win+R, type `shell:startup`, and put a shortcut to `BeaconNet.exe`
  in that folder. Add ` --no-browser` to the shortcut's target to start it quietly.
* **macOS**: System Settings > General > Login Items > add **BeaconNet**.
* **Linux**: add `BeaconNet --no-browser` to your desktop's startup applications.

Open the page again at any time at <http://127.0.0.1:8765>. Running the launcher again
just opens the page.

## Privacy and safety

* The page only listens on `127.0.0.1`, so other computers can't reach it, and every
  request needs a random token that changes each time the app starts.
* The connection to the node is SSH. The node's key is remembered the first time, and if
  it ever changes the app refuses to connect and tells you.
* Settings live in `%APPDATA%\BeaconNet` (Windows), `~/Library/Application Support/BeaconNet`
  (macOS) or `~/.config/beaconnet` (Linux). **Forget this node** on the Settings tab
  deletes them, including this computer's key.
* Notifications show subjects only, never message text.
