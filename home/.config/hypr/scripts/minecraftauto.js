const NAME = "ShadowCloneFarm";
const CLONE_COUNT = 1;
const DEBUG = false; // set true to see retry/status spam in chat

const enabled = GlobalVars.toggleBoolean(NAME);

if (enabled) {
    Chat.log("§a[ShadowCloneFarm] started");
    mainLoop();
} else {
    Chat.log("§c[ShadowCloneFarm] stopping...");
}

function debugLog(msg) {
    if (DEBUG) Chat.log(msg);
}

function mainLoop() {

    while (GlobalVars.getBoolean(NAME)) {
        waitForHealth();
        for (let i = 0; i < CLONE_COUNT; i++) {
            if (!GlobalVars.getBoolean(NAME)) break;
            spawnOneClone();
            Time.sleep(5);
        }
        if (!GlobalVars.getBoolean(NAME)) break;
        Time.sleep(325)
        closeGuiIfOpen();
        attackFor(800);
    }

    KeyBind.keyBind("key.left", false);
    KeyBind.keyBind("key.back", false);
    Chat.log("§c[ShadowCloneFarm] stopped");
}

// spams Escape for ~20ms, then falls back to clicking "Close" if a screen is still up
function closeGuiIfOpen() {
    const start = Time.time();
    while (Time.time() - start < 20) {
        if (!Hud.getOpenScreen()) return;
        KeyBind.key("key.keyboard.escape", true);
        KeyBind.key("key.keyboard.escape", false);
    }

    const screen = Hud.getOpenScreen();
    if (screen) {
        debugLog("Escape didn't close it, trying Close button");
        clickButtonByText(screen, "Close");
        Time.sleep(20);
    }
}

function spawnOneClone() {
    let attempts = 0;
    let screen = null;

    while (attempts < 15 && !screen) {
        const popo = Player.rayTraceEntity(5);
        if (!popo) { debugLog("Not looking at Mr. Popo"); attempts++; Time.sleep(5); continue; }

        Player.interactions().interactEntity(popo, false, true);
        Time.sleep(3);

        screen = waitForScreen();
        if (!screen) {
            attempts++;
            debugLog("Retrying interact (" + attempts + "/3)");
        }
    }

    if (!screen) { debugLog("Screen didn't open after 3 tries"); return; }

    clickButtonByText(screen, "Services");
    Time.sleep(120);

    screen = waitForScreen();
    if (!screen) { debugLog("Screen closed before Train"); return; }
    clickButtonByText(screen, "Train");
    Time.sleep(120);

    screen = waitForScreen();
    if (!screen) { debugLog("Screen closed before Shadow Clone"); return; }
    clickButtonByText(screen, "Shadow Clone");
}

// re-fetches the screen, retrying for up to ~300ms if momentarily null
function waitForScreen() {
    for (let i = 0; i < 15; i++) {
        const screen = Hud.getOpenScreen();
        if (screen) return screen;
        Time.sleep(120);
    }
    return null;
}

function attackFor(durationMs) {
    Chat.log("§6Attacking...");

    const ticks = Math.round(durationMs / 50);
    let tickCounter = 0;

    while (tickCounter < ticks && GlobalVars.getBoolean(NAME)) {
        const screen = Hud.getOpenScreen();

        if (screen) {
            KeyBind.keyBind("key.left", false);
            KeyBind.keyBind("key.back", false);
            Client.waitTick(1);
            tickCounter++;
            continue;
        }

        KeyBind.keyBind("key.left", true);
        KeyBind.keyBind("key.back", true);

        Player.interactions().attack();

        tickCounter++;
        Client.waitTick(1);
    }
}

function clickButtonByText(screen, text) {
    if (!screen) { debugLog("clickButtonByText called with no screen"); return false; }

    const buttons = screen.getButtonWidgets();
    for (let i = 0; i < buttons.length; i++) {
        const b = buttons[i];
        let label = "";
        try { label = b.getLabel().getString(); } catch (e) { continue; }
        if (label.toLowerCase().includes(text.toLowerCase())) {
            b.click();
            return true;
        }
    }
    debugLog("Button not found: " + text);
    return false;
}

function waitForHealth() {
    const player = Player.getPlayer();
    Chat.log(`${player.getHealth() / player.getMaxHealth()}`);
    // If health is 70% or lower, wait until it reaches 95%+
    if (GlobalVars.getBoolean(NAME) && player.getHealth() / player.getMaxHealth() <= 0.7) {
        debugLog("Low health (" + player.getHealth() + "/" + player.getMaxHealth() + "), waiting...");
        Player.interactions().attack();
        Time.sleep(100);
    }

    // Refresh check every 100ms until health is 95%+
    while (GlobalVars.getBoolean(NAME) && player.getHealth() / player.getMaxHealth() <= 0.95) {
        Chat.log(`${player.getHealth() / player.getMaxHealth()}`);
        debugLog("Healing (" + player.getHealth() + "/" + player.getMaxHealth() + "), waiting...");
        Player.interactions().attack();
        Time.sleep(100);
    }
}