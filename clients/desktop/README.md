# BeaconNet Desktop

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

You need **Python 3.9 or newer**:

| System | Get Python |
|---|---|
| Windows | [python.org/downloads](https://www.python.org/downloads/), and tick **Add python.exe to PATH**. No admin rights needed. Or install it from the Microsoft Store. |
| macOS | [python.org/downloads](https://www.python.org/downloads/) |
| Linux | it's already there (`python3`) |

Then download this folder from GitHub. Either:

* **Code > Download ZIP** on the repository page, then unzip it and open `clients/desktop`, or
* `git clone https://github.com/gorgons-1/scotland-bbs.git` and open `clients/desktop`

Start it:

| System | Start |
|---|---|
| Windows | double-click **`BeaconNet-Windows.cmd`** |
| macOS | double-click **`BeaconNet-macOS.command`** (the first time: right-click > Open) |
| Linux | `./beaconnet-linux.sh` |

The first start installs the one library it needs (`asyncssh`) for your user only, then
opens the page in your browser.

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

* **Windows**: press Win+R, type `shell:startup`, and put a shortcut to
  `BeaconNet-Windows.cmd` in that folder.
* **macOS**: System Settings > General > Login Items > add `BeaconNet-macOS.command`.
* **Linux**: add `beaconnet-linux.sh --no-browser` to your desktop's startup applications.

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
