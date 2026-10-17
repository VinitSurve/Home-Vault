# HomeVault — macOS Finder Quick Action Setup

This guide walks you through setting up the **"Send to HomeVault"** Finder Quick Action on macOS.

Once installed, you can right-click any file in Finder, click **Quick Actions** → **Send to HomeVault**, choose **Copy** or **Move**, and have the file queued immediately.

---

## Method 1: Apple Shortcuts Quick Action (Recommended)

macOS Shortcuts is the modern, native way to add Finder Quick Actions.

### Step 1: Run the CLI wrapper setup script
First, ensure the `homevault` CLI wrapper exists:
```bash
./scripts/setup_mac_cli.sh
```
This creates the executable at `~/.homevault/bin/homevault`.

---

### Step 2: Create the Shortcut

1. Open the **Shortcuts** app on your Mac (`/System/Applications/Shortcuts.app`).
2. Click **+** (New Shortcut) in the toolbar.
3. Rename the shortcut in the top title bar to: **`Send to HomeVault`**.
4. In the right sidebar, open the **Shortcut Details** tab (the `(i)` icon):
   - Check **Use as Quick Action**.
   - Under **Services Menu**, ensure **Finder** is checked.
   - Set **Receive**: `Files` from `Quick Actions`.
   - If there's no input: `Continue`.
5. In the action search bar on the right, find and add: **Choose from Menu**.
   - Prompt: `Choose transfer mode:`
   - Menu items:
     - Item 1: `Copy to Windows`
     - Item 2: `Move to Windows`
6. Inside the **`Copy to Windows`** block, search and drag in: **Run Shell Script**.
   - Shell: `/bin/bash`
   - Pass input: **as arguments**
   - Script:
     ```bash
     ~/.homevault/bin/homevault send --mode copy "$@"
     ```
7. Inside the **`Move to Windows`** block, search and drag in: **Run Shell Script**.
   - Shell: `/bin/bash`
   - Pass input: **as arguments**
   - Script:
     ```bash
     ~/.homevault/bin/homevault send --mode move "$@"
     ```
8. After the menu (at the bottom of the shortcut), search and add: **Show Notification**.
   - Message: `Files queued for delivery to Windows PC.`
   - Title: `HomeVault`

Close the Shortcuts editor. The shortcut saves automatically.

---

### Step 3: Test in Finder

1. Open any Finder window.
2. Right-click any file.
3. Hover over **Quick Actions** → click **Send to HomeVault**.
4. Select **Copy to Windows** or **Move to Windows**.
5. You will immediately see a macOS notification confirming the file has been queued.

---

## Method 2: Automator Quick Action (Alternative)

If you prefer using macOS Automator:

1. Open **Automator** (`/System/Applications/Automator.app`).
2. Choose **New Document** → Select **Quick Action** → Click **Choose**.
3. At the top of the workflow window, configure:
   - **Workflow receives current**: `files or folders` in `Finder.app`.
4. From the Actions library on the left, drag **Run Shell Script** into the right pane.
   - Shell: `/bin/bash`
   - Pass input: **as arguments**
   - Script:
     ```bash
     /bin/bash "${HOME}/Vinit/Home Vault/integrations/macos/send_to_homevault.sh" "$@"
     ```
5. File → **Save** → Name it **`Send to HomeVault`**.

The Quick Action will now appear in your Finder right-click context menu.
