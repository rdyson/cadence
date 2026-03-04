/**
 * Cadence — app.js
 * Handles auth (Cognito), config loading, UI rendering, and checkbox state.
 */

// ── State ────────────────────────────────────────────────────────────────────

let cadence = null;       // Loaded from cadence.json
let state = {};           // { userId: { itemTitle: true/false } }
let currentUser = null;   // { id, name, email }
let accessToken = null;

// ── Cognito Auth ─────────────────────────────────────────────────────────────

function cognitoEndpoint() {
    const { region } = cadence.aws;
    return `https://cognito-idp.${region}.amazonaws.com/`;
}

async function cognitoRequest(body) {
    const resp = await fetch(cognitoEndpoint(), {
        method: "POST",
        headers: { "Content-Type": "application/x-amz-json-1.1" },
        body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw data;
    return data;
}

async function signIn(email, password) {
    return cognitoRequest({
        AuthFlow: "USER_PASSWORD_AUTH",
        ClientId: cadence.aws.cognito_client_id,
        AuthParameters: { USERNAME: email, PASSWORD: password },
        "__type": "InitiateAuth",
    });
}

async function respondToNewPasswordChallenge(session, email, newPassword) {
    return cognitoRequest({
        ChallengeName: "NEW_PASSWORD_REQUIRED",
        ClientId: cadence.aws.cognito_client_id,
        ChallengeResponses: { USERNAME: email, NEW_PASSWORD: newPassword },
        Session: session,
        "__type": "RespondToAuthChallenge",
    });
}

function storeTokens(authResult) {
    accessToken = authResult.AccessToken;
    const expiry = Date.now() + (authResult.ExpiresIn * 1000);
    sessionStorage.setItem("cadence_access_token", accessToken);
    sessionStorage.setItem("cadence_token_expiry", expiry);
    if (authResult.RefreshToken) {
        sessionStorage.setItem("cadence_refresh_token", authResult.RefreshToken);
    }
}

function loadStoredToken() {
    const token = sessionStorage.getItem("cadence_access_token");
    const expiry = parseInt(sessionStorage.getItem("cadence_token_expiry") || "0");
    if (token && Date.now() < expiry) {
        accessToken = token;
        return true;
    }
    return false;
}

function clearTokens() {
    accessToken = null;
    sessionStorage.removeItem("cadence_access_token");
    sessionStorage.removeItem("cadence_token_expiry");
    sessionStorage.removeItem("cadence_refresh_token");
}

function getUserFromToken(token) {
    try {
        const payload = JSON.parse(atob(token.split(".")[1]));
        const email = payload.email || payload["cognito:username"] || "";
        const user = cadence.users.find(u =>
            u.email?.toLowerCase() === email.toLowerCase() ||
            u.id === payload["cognito:username"]
        );
        return user || { id: payload["cognito:username"], name: email, email };
    } catch {
        return null;
    }
}

// ── API ───────────────────────────────────────────────────────────────────────

async function apiGet(path) {
    const resp = await fetch(cadence.aws.api_url + path, {
        headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (resp.status === 401) { signOut(); return null; }
    return resp.json();
}

async function apiPost(path, body) {
    const resp = await fetch(cadence.aws.api_url + path, {
        method: "POST",
        headers: {
            Authorization: `Bearer ${accessToken}`,
            "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
    });
    if (resp.status === 401) { signOut(); return null; }
    return resp.json();
}

async function loadState() {
    const data = await apiGet("/state");
    if (data) state = data.users || {};
}

// ── Pace calculation ──────────────────────────────────────────────────────────

const INTERVAL_DAYS = { week: 7, month: 30, day: 1, year: 365, sprint: 14, quarter: 91 };

function getPaceStatus(userId) {
    if (!cadence.completion_date) return null;
    const completion = new Date(cadence.completion_date);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const daysPerInterval = INTERVAL_DAYS[cadence.interval] || 7;
    const totalDays = cadence.periods.length * daysPerInterval;
    const daysRemaining = Math.max(0, (completion - today) / (1000 * 60 * 60 * 24));
    const daysElapsed = Math.max(0, totalDays - daysRemaining);
    const expectedPct = Math.min(1, daysElapsed / totalDays);
    const { checked, total } = countUserProgress(userId);
    const actualPct = total > 0 ? checked / total : 0;
    const diff = actualPct - expectedPct;
    if (expectedPct <= 0) return null; // Not started yet
    if (diff >= -0.05)  return { label: "On track",        icon: "✓", cls: "pace-good" };
    if (diff >= -0.15)  return { label: "Slightly behind", icon: "~", cls: "pace-warn" };
    return                     { label: "Catch up needed", icon: "!", cls: "pace-bad"  };
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function show(id) { document.getElementById(id).classList.remove("hidden"); }
function hide(id) { document.getElementById(id).classList.add("hidden"); }
function esc(s) {
    const d = document.createElement("div");
    d.textContent = String(s || "");
    return d.innerHTML;
}

// ── Header ────────────────────────────────────────────────────────────────────

function renderHeader() {
    document.title = cadence.name;
    document.getElementById("project-name").textContent = cadence.name;
    document.getElementById("project-desc").textContent = cadence.description || "";
    document.getElementById("login-title").textContent = cadence.name;
    document.getElementById("signed-in-as").textContent = currentUser.name;
}

// ── Countdown ─────────────────────────────────────────────────────────────────

function renderCountdown() {
    const el = document.getElementById("days-remaining");
    const sub = document.getElementById("completion-date-label");
    if (!cadence.completion_date) { el.textContent = "—"; return; }
    const target = new Date(cadence.completion_date);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diff = Math.ceil((target - today) / (1000 * 60 * 60 * 24));
    el.textContent = diff > 0 ? diff : "0";
    sub.textContent = target.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// ── User progress cards ───────────────────────────────────────────────────────

function countUserProgress(userId) {
    const userChecks = state[userId] || {};
    let checked = 0, checkedHours = 0, totalHours = 0;
    const total = cadence.total_items;
    for (const period of cadence.periods) {
        for (const item of period.items) {
            if (userChecks[item.title]) { checked++; checkedHours += item.hours || 0; }
            totalHours += item.hours || 0;
        }
    }
    return { checked, total, checkedHours, totalHours };
}

function renderUserProgress() {
    const container = document.getElementById("user-progress-cards");
    container.innerHTML = "";
    for (const user of cadence.users) {
        const isMe = user.id === currentUser.id;
        const { checked, total, checkedHours, totalHours } = countUserProgress(user.id);
        const pct = total > 0 ? Math.round((checked / total) * 100) : 0;
        const pace = getPaceStatus(user.id);

        const card = document.createElement("div");
        card.className = `user-card${isMe ? " is-me" : ""}`;
        card.innerHTML = `
            <div class="user-name">
                ${esc(user.name)}
                ${isMe ? '<span class="you-badge">You</span>' : ""}
                ${pace ? `<span class="pace-badge ${pace.cls}">${pace.icon} ${pace.label}</span>` : ""}
            </div>
            <div class="progress-bar-wrap">
                <div class="progress-bar-fill" style="width:${pct}%"></div>
            </div>
            <div class="progress-text">
                <span>${checked} / ${total} items (${pct}%)</span>
                ${totalHours > 0 ? `<span>${checkedHours.toFixed(1)}h / ${totalHours.toFixed(1)}h</span>` : ""}
            </div>
        `;
        container.appendChild(card);
    }
}

// ── Current period detection ──────────────────────────────────────────────────

function getCurrentPeriodNumber() {
    if (!cadence.completion_date) return cadence.periods[0]?.number;
    const completion = new Date(cadence.completion_date);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const daysPerInterval = INTERVAL_DAYS[cadence.interval] || 7;
    const totalPeriods = cadence.periods.length;
    const totalDays = totalPeriods * daysPerInterval;
    const daysRemaining = Math.max(0, (completion - today) / (1000 * 60 * 60 * 24));
    const daysElapsed = Math.max(0, totalDays - daysRemaining);
    const periodIndex = Math.min(totalPeriods - 1, Math.floor(daysElapsed / daysPerInterval));
    return cadence.periods[periodIndex]?.number ?? cadence.periods[0]?.number;
}

// ── Periods ───────────────────────────────────────────────────────────────────

function renderPeriods() {
    const container = document.getElementById("periods-container");
    container.innerHTML = "";
    const currentPeriodNum = getCurrentPeriodNumber();

    for (const period of cadence.periods) {
        const isCurrent = period.number === currentPeriodNum;
        const periodEl = document.createElement("div");
        periodEl.className = `period${isCurrent ? " current-period open" : ""}`;
        periodEl.dataset.period = period.number;

        // Per-user summary badges
        const userSummary = cadence.users.map(user => {
            const userChecks = state[user.id] || {};
            const done = period.items.filter(i => userChecks[i.title]).length;
            const total = period.items.length;
            const allDone = done === total && total > 0;
            return `<span class="period-user-badge${allDone ? " done" : ""}">${esc(user.name)}: ${done}/${total}</span>`;
        }).join("");

        // Table header user columns
        const userThs = cadence.users.map(u =>
            `<th class="check-col">${esc(u.name)}</th>`
        ).join("");

        // Item rows
        const rows = period.items.map(item => {
            const userCells = cadence.users.map(user => {
                const isMe = user.id === currentUser.id;
                const checked = !!(state[user.id] || {})[item.title];
                return `<td class="check-col">
                    <button
                        class="check-btn${checked ? " checked" : ""}${!isMe ? " readonly" : ""}"
                        data-item="${esc(item.title)}"
                        data-user="${esc(user.id)}"
                        data-me="${isMe}"
                        aria-label="${checked ? "Uncheck" : "Check"} ${esc(item.title)}"
                    >${checked ? "✓" : ""}</button>
                </td>`;
            }).join("");

            // Title — link if URL present
            const titleHtml = item.url
                ? `<a href="${esc(item.url)}" target="_blank" rel="noopener" class="item-link">${esc(item.title)}</a>`
                : esc(item.title);

            const hoursHtml = item.hours
                ? `<span class="hours-badge">${item.hours}h</span>`
                : "";

            // Mobile: user check cells as labelled inline badges
            const mobileChecks = cadence.users.map(user => {
                const isMe = user.id === currentUser.id;
                const checked = !!(state[user.id] || {})[item.title];
                return `<span class="mobile-check${checked ? " checked" : ""}${!isMe ? " readonly" : ""}"
                    data-item="${esc(item.title)}"
                    data-user="${esc(user.id)}"
                    data-me="${isMe}"
                    role="button" tabindex="0"
                    aria-label="${checked ? "Uncheck" : "Check"} ${esc(item.title)} for ${esc(user.name)}"
                >${checked ? "✓" : "○"} ${esc(user.name)}</span>`;
            }).join("");

            return `<tr>
                <td class="item-title-cell">
                    <span class="item-title">${titleHtml}</span>
                    ${hoursHtml ? `<span class="hours-inline">${hoursHtml}</span>` : ""}
                    <div class="mobile-checks">${mobileChecks}</div>
                </td>
                <td class="hours-cell">${hoursHtml}</td>
                ${userCells}
            </tr>`;
        }).join("");

        periodEl.innerHTML = `
            <div class="period-header">
                <div class="period-header-left">
                    <span class="period-chevron">▶</span>
                    <div>
                        <span class="period-label">${esc(period.label)}</span>
                        ${period.description ? `<div class="period-desc">${esc(period.description)}</div>` : ""}
                    </div>
                    ${period.total_hours > 0 ? `<span class="period-hours">${period.total_hours}h</span>` : ""}
                </div>
                <div class="period-user-summary">${userSummary}</div>
            </div>
            <div class="period-items">
                <table>
                    <thead><tr>
                        <th>Item</th>
                        <th class="hours-cell">Hours</th>
                        ${userThs}
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
        container.appendChild(periodEl);
    }

    // Event delegation — clicks on period headers and checkboxes
    container.addEventListener("click", handleContainerClick);
    container.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") handleContainerClick(e);
    });
}

function handleContainerClick(e) {
    const header = e.target.closest(".period-header");
    if (header) {
        header.closest(".period").classList.toggle("open");
        return;
    }
    const btn = e.target.closest(".check-btn, .mobile-check");
    if (btn && btn.dataset.me === "true") handleCheck(btn);
}

// ── Checkbox interaction ──────────────────────────────────────────────────────

async function handleCheck(btn) {
    const itemTitle = btn.dataset.item;
    const userId = btn.dataset.user;
    const newChecked = !btn.classList.contains("checked");

    // Optimistic update — all matching buttons for this item + user
    if (!state[userId]) state[userId] = {};
    state[userId][itemTitle] = newChecked;

    document.querySelectorAll(`[data-item="${CSS.escape(itemTitle)}"][data-user="${CSS.escape(userId)}"]`)
        .forEach(el => {
            el.classList.toggle("checked", newChecked);
            if (el.classList.contains("check-btn")) el.textContent = newChecked ? "✓" : "";
            if (el.classList.contains("mobile-check")) el.textContent = (newChecked ? "✓ " : "○ ") + cadence.users.find(u => u.id === userId)?.name;
        });

    updatePeriodSummaries();
    renderUserProgress();

    // Check for 100% completion (own items)
    if (newChecked && userId === currentUser.id) {
        const { checked, total } = countUserProgress(userId);
        if (checked === total) triggerCompletion();
    }

    // Persist
    try {
        await apiPost("/state", { item: itemTitle, checked: newChecked });
    } catch {
        // Rollback
        state[userId][itemTitle] = !newChecked;
        document.querySelectorAll(`[data-item="${CSS.escape(itemTitle)}"][data-user="${CSS.escape(userId)}"]`)
            .forEach(el => {
                el.classList.toggle("checked", !newChecked);
                if (el.classList.contains("check-btn")) el.textContent = !newChecked ? "✓" : "";
                if (el.classList.contains("mobile-check")) el.textContent = (!newChecked ? "✓ " : "○ ") + cadence.users.find(u => u.id === userId)?.name;
            });
        updatePeriodSummaries();
        renderUserProgress();
    }
}

function updatePeriodSummaries() {
    document.querySelectorAll(".period").forEach(periodEl => {
        const periodNum = parseInt(periodEl.dataset.period);
        const period = cadence.periods.find(p => p.number === periodNum);
        if (!period) return;
        const summaryEl = periodEl.querySelector(".period-user-summary");
        if (!summaryEl) return;
        summaryEl.innerHTML = cadence.users.map(user => {
            const userChecks = state[user.id] || {};
            const done = period.items.filter(i => userChecks[i.title]).length;
            const total = period.items.length;
            const allDone = done === total && total > 0;
            return `<span class="period-user-badge${allDone ? " done" : ""}">${esc(user.name)}: ${done}/${total}</span>`;
        }).join("");
    });
}

// ── Completion celebration ────────────────────────────────────────────────────

function triggerCompletion() {
    const key = `cadence_celebrated_${currentUser.id}`;
    if (sessionStorage.getItem(key)) return;
    sessionStorage.setItem(key, "1");

    launchConfetti();

    const messages = [
        "Every item. Every week. Done.",
        "That's what consistency looks like.",
        "You showed up. Every single time.",
        "The work is done. Go book the exam.",
        "Nothing left unchecked.",
    ];
    const msg = messages[Math.floor(Math.random() * messages.length)];

    const overlay = document.createElement("div");
    overlay.className = "celebration-overlay";
    overlay.innerHTML = `
        <div class="celebration-card">
            <div class="celebration-emoji">🏆</div>
            <h2>100% Complete</h2>
            <p>${msg}</p>
            <button onclick="this.closest('.celebration-overlay').remove()">Close</button>
        </div>
    `;
    document.body.appendChild(overlay);
    setTimeout(() => overlay.classList.add("visible"), 50);
}

function launchConfetti() {
    const canvas = document.createElement("canvas");
    canvas.className = "confetti-canvas";
    document.body.appendChild(canvas);
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    const ctx = canvas.getContext("2d");

    const colours = ["#4f46e5","#10b981","#f59e0b","#ef4444","#8b5cf6","#06b6d4","#f97316"];
    const particles = Array.from({ length: 120 }, () => ({
        x: Math.random() * canvas.width,
        y: -10 - Math.random() * 100,
        r: 4 + Math.random() * 6,
        d: 1.5 + Math.random() * 2,
        colour: colours[Math.floor(Math.random() * colours.length)],
        tilt: Math.random() * 10 - 5,
        tiltAngle: 0,
        tiltSpeed: 0.05 + Math.random() * 0.1,
    }));

    let frame = 0;
    function draw() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        particles.forEach(p => {
            p.tiltAngle += p.tiltSpeed;
            p.y += p.d;
            p.x += Math.sin(p.tiltAngle) * 1.2;
            p.tilt = Math.sin(p.tiltAngle) * 12;
            ctx.beginPath();
            ctx.fillStyle = p.colour;
            ctx.globalAlpha = Math.max(0, 1 - frame / 160);
            ctx.ellipse(p.x, p.y, p.r, p.r * 0.5, p.tilt, 0, Math.PI * 2);
            ctx.fill();
        });
        frame++;
        if (frame < 180) requestAnimationFrame(draw);
        else canvas.remove();
    }
    requestAnimationFrame(draw);
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function signOut() {
    clearTokens();
    currentUser = null;
    state = {};
    hide("app");
    show("login-screen");
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
    try {
        const resp = await fetch("cadence.json");
        cadence = await resp.json();
    } catch {
        document.body.innerHTML = '<p style="padding:40px;color:#dc2626">Error loading cadence.json — run <code>python scripts/build.py</code> first.</p>';
        return;
    }

    document.getElementById("login-title").textContent = cadence.name;
    document.getElementById("login-desc").textContent = cadence.description || "Keep the pace.";

    if (loadStoredToken()) {
        currentUser = getUserFromToken(accessToken);
        if (currentUser) { await launchApp(); return; }
    }

    show("login-screen");
    setupLogin();
}

function setupLogin() {
    let pendingSession = null;
    let pendingEmail = null;

    const form = document.getElementById("login-form");
    const errorEl = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");

    form.addEventListener("submit", async e => {
        e.preventDefault();
        errorEl.classList.add("hidden");
        btn.disabled = true;
        btn.textContent = "Signing in…";

        const email = document.getElementById("login-email").value.trim();
        const password = document.getElementById("login-password").value;

        try {
            const result = await signIn(email, password);
            if (result.ChallengeName === "NEW_PASSWORD_REQUIRED") {
                pendingSession = result.Session;
                pendingEmail = email;
                document.getElementById("login-change-password").classList.remove("hidden");
                document.getElementById("new-password-section").classList.remove("hidden");
                btn.disabled = false;
                btn.textContent = "Sign in";
                return;
            }
            storeTokens(result.AuthenticationResult);
            currentUser = getUserFromToken(result.AuthenticationResult.AccessToken);
            await launchApp();
        } catch (err) {
            const msg = err.__type === "NotAuthorizedException"
                ? "Incorrect email or password."
                : (err.message || "Sign in failed.");
            errorEl.textContent = msg;
            errorEl.classList.remove("hidden");
            btn.disabled = false;
            btn.textContent = "Sign in";
        }
    });

    document.getElementById("set-password-btn").addEventListener("click", async () => {
        const newPw = document.getElementById("new-password").value;
        try {
            const result = await respondToNewPasswordChallenge(pendingSession, pendingEmail, newPw);
            storeTokens(result.AuthenticationResult);
            currentUser = getUserFromToken(result.AuthenticationResult.AccessToken);
            await launchApp();
        } catch (err) {
            const errorEl = document.getElementById("login-error");
            errorEl.textContent = err.message || "Failed to set password.";
            errorEl.classList.remove("hidden");
        }
    });
}

async function launchApp() {
    hide("login-screen");
    await loadState();
    renderHeader();
    renderCountdown();
    renderUserProgress();
    renderPeriods();
    document.getElementById("sign-out-btn").addEventListener("click", signOut);
    show("app");
}

init();
