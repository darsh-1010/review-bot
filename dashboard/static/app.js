// PR titles and issue text come from users, so everything is written with textContent, never innerHTML.
const $ = (id) => document.getElementById(id);
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids);
  return e;
};

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                               body: body ? JSON.stringify(body) : undefined });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

// ---------- 3D background (Three.js): wireframe crystal + particle field, mouse parallax ----------
function scene3d() {
  const canvas = $("bg");
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
  cam.position.z = 8;

  const crystal = new THREE.Mesh(
    new THREE.IcosahedronGeometry(2.4, 1),
    new THREE.MeshBasicMaterial({ color: 0x7c5cff, wireframe: true, transparent: true, opacity: 0.55 }));
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.2, 0),
    new THREE.MeshBasicMaterial({ color: 0x22d3ee, wireframe: true, transparent: true, opacity: 0.8 }));
  const group = new THREE.Group();
  group.add(crystal, core);
  scene.add(group);

  const N = 900, pos = new Float32Array(N * 3);
  for (let i = 0; i < pos.length; i++) pos[i] = (Math.random() - 0.5) * 30;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const stars = new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.045, color: 0x9fb0ff, transparent: true, opacity: 0.7 }));
  scene.add(stars);

  const mouse = { x: 0, y: 0 };
  addEventListener("pointermove", (e) => { mouse.x = e.clientX / innerWidth - 0.5; mouse.y = e.clientY / innerHeight - 0.5; });
  const resize = () => {
    renderer.setSize(innerWidth, innerHeight, false);
    cam.aspect = innerWidth / innerHeight; cam.updateProjectionMatrix();
    const narrow = innerWidth < 900;  // push the crystal back so it doesn't cover content
    group.position.set(narrow ? 0 : 3.2, narrow ? -1 : 0.4, narrow ? -4 : 0);
  };
  addEventListener("resize", resize); resize();

  const t0 = performance.now();
  (function frame() {
    const t = (performance.now() - t0) / 1000;
    if (!reduced) {
      crystal.rotation.set(t * 0.15, t * 0.2, 0);
      core.rotation.set(-t * 0.3, -t * 0.25, 0);
      stars.rotation.y = t * 0.012;
    }
    cam.position.x += (mouse.x * 2 - cam.position.x) * 0.04;   // parallax
    cam.position.y += (-mouse.y * 2 - cam.position.y) * 0.04;
    cam.lookAt(0, 0, 0);
    renderer.render(scene, cam);
    requestAnimationFrame(frame);
  })();
  return group;
}

// ---------- stat cards: GSAP count-up, 3D tilt, cursor spotlight ----------
function card(label, value, fmt) {
  const d = el("div", { className: "card glass" }, el("small", { textContent: label }));
  const b = el("b", { textContent: fmt(0) });
  d.append(b);
  $("cards").append(d);

  const o = { v: 0 };
  gsap.to(o, { v: value, duration: reduced ? 0 : 1.6, ease: "power3.out", delay: 0.3,
               onUpdate: () => (b.textContent = fmt(o.v)), onComplete: () => (b.textContent = fmt(value)) });
  if (reduced) return;
  const rx = gsap.quickTo(d, "rotationX", { duration: 0.4 }), ry = gsap.quickTo(d, "rotationY", { duration: 0.4 });
  d.addEventListener("pointermove", (e) => {
    const r = d.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    ry((x - 0.5) * 18); rx((0.5 - y) * 18);
    d.style.setProperty("--mx", x * 100 + "%"); d.style.setProperty("--my", y * 100 + "%");
  });
  d.addEventListener("pointerleave", () => { rx(0); ry(0); });
}

const tag = (n, cls) => el("span", { className: "tag " + cls + (n ? "" : " zero"), textContent: n });
const SEV_CLASS = { high: "t-hi", medium: "t-md", low: "t-lo" };

// ---------- reviews: filters, table, charts ----------
let reviews = [], sevChart, costChart;

function filtered() {
  const q = $("q").value.trim().toLowerCase(), repo = $("frepo").value, model = $("fmodel").value, sev = $("fsev").value;
  return reviews.filter((r) =>
    (!repo || r.repo === repo) && (!model || r.model === model) && (!sev || r[sev] > 0) &&
    (!q || `${r.title} ${r.repo} #${r.number} ${(r.issues || []).map((i) => i.message).join(" ")}`.toLowerCase().includes(q)));
}

function render() {
  const rows = filtered();
  $("rows").replaceChildren(...rows.map((r) => {
    const cells = [r.time.slice(0, 19).replace("T", " "), r.repo, "#" + r.number, r.title, r.model,
                   tag(r.high, "t-hi"), tag(r.medium, "t-md"), tag(r.low, "t-lo"),
                   r.cost_usd.toFixed(4), (r.duration_ms / 1000).toFixed(1)];
    const tr = el("tr", {}, ...cells.map((v, i) => el("td", { className: i >= 5 ? "num" : "" }, v)));
    tr.onclick = () => openReview(r);
    return tr;
  }));
  sevChart.data.datasets[0].data = ["high", "medium", "low"].map((s) => rows.reduce((n, r) => n + r[s], 0));
  sevChart.update();
  const chrono = [...rows].reverse();
  costChart.data.labels = chrono.map((r) => r.time.slice(5, 16).replace("T", " "));
  costChart.data.datasets[0].data = chrono.map((r) => r.cost_usd);
  costChart.update();
}

// ---------- review detail with a thumbs up/down per issue (feeds the next review's prompt) ----------
function openReview(r) {
  const box = $("box");
  const close = el("button", { textContent: "Close" });
  close.onclick = () => ($("modal").style.display = "none");
  const link = el("a", { href: r.url || "#", target: "_blank", rel: "noopener", textContent: `${r.repo} #${r.number}` });
  box.replaceChildren(el("h3", { textContent: r.title }), el("div", { className: "meta" }, link, ` · ${r.model} · $${r.cost_usd.toFixed(4)}`));
  if (!(r.issues || []).length) box.append(el("p", { textContent: "No issues found." }));
  (r.issues || []).forEach((i, idx) => {
    const thumbs = ["up", "down"].map((v) => {
      const b = el("button", { className: "thumb " + v, textContent: v === "up" ? "👍 Useful" : "👎 Not useful" });
      const sync = () => b.classList.toggle("on", (r.votes || {})[idx] === v);
      sync();
      b.onclick = async () => {
        const next = (r.votes || {})[idx] === v ? "" : v;  // click again to undo
        try { r.votes = (await api("POST", "/api/feedback", { sk: r.sk, idx, vote: next })).votes; }
        catch (e) { alert(e.message); }
        thumbs.forEach((t) => t.sync());
      };
      b.sync = sync;
      return b;
    });
    box.append(el("div", { className: "issue" },
      el("div", { className: "meta" }, tag(i.severity, SEV_CLASS[i.severity]), ` ${i.category} · ${i.path}:${i.line}`),
      el("p", { textContent: i.message }), ...thumbs));
  });
  box.append(el("div", { className: "row", style: "margin-top:14px" }, close));
  $("modal").style.display = "flex";
  if (!reduced) gsap.from(box, { y: 30, opacity: 0, scale: 0.97, duration: 0.35, ease: "power3.out" });
}
$("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").style.display = "none"; };

// ---------- GitHub accounts: the token is sent once over HTTPS, checked against GitHub and stored encrypted; it is never shown again ----------
async function loadAccounts() {
  const accts = await api("GET", "/api/accounts");
  $("accts").replaceChildren(...accts.map((a) => {
    const del = el("button", { className: "danger", textContent: "Remove" });
    del.onclick = () => confirm(`Remove ${a.login} and its stored token?`) &&
      actAcct(() => api("DELETE", "/api/accounts", { login: a.login }), "Removed " + a.login);
    return el("div", { className: "repo" }, el("b", { textContent: a.login }), del);
  }));
  const keep = $("account").value;
  $("account").replaceChildren(el("option", { value: "", textContent: accts.length ? "GitHub account" : "(add an account first)" }),
    ...accts.map((a) => el("option", { value: a.login, textContent: a.login })));
  $("account").value = keep;
  if (!$("account").value && accts.length === 1) $("account").value = accts[0].login;  // only one: pre-pick it
  await loadPicker();
}

// the repo dropdown: ask GitHub (through the dashboard) what the chosen account can reach
async function loadPicker() {
  const acct = $("account").value, sel = $("repo");
  const opt = (value, text) => el("option", { value, textContent: text });
  sel.replaceChildren(opt("", acct ? "Loading repos..." : "Pick an account first"));
  if (!acct) return;
  try {
    const names = await api("GET", `/api/accounts/${encodeURIComponent(acct)}/repos`);
    sel.replaceChildren(opt("", names.length ? "Choose a repo" : "No repos left to connect"), ...names.map((n) => opt(n, n)));
  } catch (e) {
    sel.replaceChildren(opt("", "Could not load repos"));
    $("msg").textContent = e.message;
  }
}
$("account").onchange = loadPicker;

async function actAcct(fn, ok) {
  $("amsg").style.color = "";
  try { await fn(); $("amsg").textContent = ok; $("amsg").style.color = "#34d399"; await loadAccounts(); }
  catch (e) { $("amsg").textContent = e.message; }
}

$("addacct").onclick = () => actAcct(async () => {
  const token = $("token").value;
  $("token").value = "";  // gone from the page immediately
  await api("POST", "/api/accounts", { token });
}, "Account added. Pick it when connecting a repo.");

// ---------- repositories: connect (creates the GitHub webhook), settings, remove ----------
async function loadRepos() {
  const repos = await api("GET", "/api/repos");
  $("repos").replaceChildren(...repos.map((r) => {
    const model = el("select", {}, ...["claude-haiku-5-5", "claude-sonnet-5-5"].map((m) => el("option", { value: m, textContent: m })));
    model.value = r.model;
    const cap = el("input", { type: "number", min: 0.01, max: 10, step: 0.05, value: (r.max_cost_micro / 1e6).toFixed(2) });
    const save = el("button", { textContent: "Save" }), del = el("button", { className: "danger", textContent: "Remove" });
    save.onclick = () => act(() => api("PUT", "/api/repos", { repo: r.repo, model: model.value, max_cost_usd: +cap.value }), "Saved " + r.repo);
    del.onclick = () => confirm(`Remove ${r.repo}? Its GitHub webhook is deleted.`) &&
      act(() => api("DELETE", "/api/repos", { repo: r.repo }), "Removed " + r.repo);
    return el("div", { className: "repo" }, el("b", { textContent: r.repo + (r.account ? " · " + r.account : "") }), model, "max $", cap, save, del);
  }));
  const skeep = $("srepo").value;
  $("srepo").replaceChildren(el("option", { value: "", textContent: repos.length ? "Choose a connected repo" : "Connect a repo first" }),
    ...repos.map((r) => el("option", { value: r.repo, textContent: r.repo })));
  $("srepo").value = skeep;
  const keep = $("frepo").value;
  $("frepo").replaceChildren(el("option", { value: "", textContent: "All repos" }),
    ...[...new Set([...repos.map((r) => r.repo), ...reviews.map((r) => r.repo)])].sort().map((n) => el("option", { value: n, textContent: n })));
  $("frepo").value = keep;
}

async function act(fn, ok) {
  $("msg").style.color = "";
  try { await fn(); $("msg").textContent = ok; $("msg").style.color = "#34d399"; await loadRepos(); await loadPicker(); }
  catch (e) { $("msg").textContent = e.message; }
}

$("connect").onclick = () => act(async () => {
  await api("POST", "/api/repos", { repo: $("repo").value, account: $("account").value, model: $("model").value, max_cost_usd: +$("cap").value });
  $("repo").value = "";
}, "Connected: open a pull request there to try it.");

// ---------- tabs ----------
function showTab(name) {
  for (const t of ["reviews", "scan"]) {
    $("tab-" + t).hidden = t !== name;
    $("tabbtn-" + t).classList.toggle("on", t === name);
  }
  if (name === "scan") loadScans();
}
$("tabbtn-reviews").onclick = () => showTab("reviews");
$("tabbtn-scan").onclick = () => showTab("scan");

// ---------- repo scan: queue a job, poll while one is running, show score + checklist + issues with fixes ----------
let scans = [], scanTimer;
const when = (s) => s.time.slice(0, 19).replace("T", " ");

async function loadScans() {
  clearTimeout(scanTimer);
  try { scans = await api("GET", "/api/scans"); } catch (e) { $("smsg").textContent = e.message; return; }
  renderScans();
  if (scans.some((s) => s.status === "queued" || s.status === "running") && !$("tab-scan").hidden)
    scanTimer = setTimeout(loadScans, 5000);  // stop polling when nothing is running or the tab is hidden
}

function renderScans() {
  if (!scans.length) return $("srows").replaceChildren(el("tr", {}, el("td", { colSpan: 9, textContent: "No scans yet." })));
  $("srows").replaceChildren(...scans.map((s) => {
    const done = s.status === "done";
    const n = (k) => (done ? (s.issues || []).filter((i) => i.severity === k).length : "");
    const cells = [when(s), s.repo, el("span", { className: "st-" + s.status, textContent: s.status }),
                   done ? s.score : "", n("high"), n("medium"), n("low"),
                   done ? `${s.files_scanned}/${s.files_total}` : "", done ? (s.cost_micro / 1e6).toFixed(4) : ""];
    const tr = el("tr", {}, ...cells.map((v, i) => el("td", { className: i >= 3 ? "num" : "" }, v)));
    tr.onclick = () => openScan(s);
    return tr;
  }));
}

function openScan(s) {
  const box = $("box");
  const close = el("button", { textContent: "Close" });
  close.onclick = () => ($("modal").style.display = "none");
  box.replaceChildren(el("h3", { textContent: `${s.repo} · scan` }),
    el("div", { className: "meta", textContent: `${when(s)} UTC · ${s.model}` + (s.branch ? ` · branch ${s.branch}` : "") }));
  if (s.status !== "done") {
    box.append(el("p", { className: "st-" + s.status,
                         textContent: s.status === "failed" ? "Scan failed: " + (s.error || "unknown error") : "Scan is " + s.status + "... this takes a few minutes." }),
               el("div", { className: "row" }, close));
  } else {
    const issues = s.issues || [], count = (k) => issues.filter((i) => i.severity === k).length;
    box.append(
      el("div", { className: "row", style: "align-items:flex-end;gap:24px;margin-top:10px" },
        el("div", {}, el("small", { textContent: "HEALTH SCORE" }), el("div", { className: "score", textContent: s.score })),
        el("div", {}, tag(count("high"), "t-hi"), " high  ", tag(count("medium"), "t-md"), " medium  ", tag(count("low"), "t-lo"), " low"),
        el("div", { className: "meta", textContent: `${s.files_scanned} of ${s.files_total} files scanned · $${(s.cost_micro / 1e6).toFixed(4)} · ${(s.duration_ms / 1000).toFixed(0)}s` })));
    (s.notes || []).forEach((n) => box.append(el("p", { className: "msg", textContent: "Note: " + n })));
    (s.summaries || []).forEach((t) => box.append(el("p", { style: "color:var(--muted)", textContent: t })));
    box.append(el("h3", { textContent: "Best-practice checklist", style: "margin-top:18px" }));
    (s.checks || []).forEach((c) => box.append(el("div", { className: "chk" },
      el("span", { className: c.ok ? "ok" : "bad", textContent: c.ok ? "✓" : "✗" }),
      el("div", {}, c.name, c.ok ? "" : el("div", { className: "sug", textContent: c.hint })))));
    box.append(el("h3", { textContent: `Issues (${issues.length})`, style: "margin-top:18px" }));
    if (!issues.length) box.append(el("p", { textContent: "No issues found." }));
    issues.forEach((i) => {
      const href = `https://github.com/${s.repo}/blob/${encodeURIComponent(s.branch || "HEAD")}/${i.path.split("/").map(encodeURIComponent).join("/")}#L${i.line}`;
      box.append(el("div", { className: "issue" },
        el("div", { className: "meta" }, tag(i.severity, SEV_CLASS[i.severity]), ` ${i.category} · `,
           el("a", { href, target: "_blank", rel: "noopener", textContent: `${i.path}:${i.line}` })),
        el("p", { textContent: i.message }),
        i.suggestion ? el("div", { className: "sug", textContent: "Fix: " + i.suggestion }) : ""));
    });
    box.append(el("div", { className: "row", style: "margin-top:14px" }, close));
  }
  $("modal").style.display = "flex";
}

$("startscan").onclick = async () => {
  $("smsg").style.color = "";
  try {
    await api("POST", "/api/scans", { repo: $("srepo").value, model: $("smodel").value, max_cost_usd: +$("scap").value });
    $("smsg").textContent = "Scan queued. It usually takes 1-5 minutes."; $("smsg").style.color = "#34d399";
    await loadScans();
  } catch (e) { $("smsg").textContent = e.message; }
};

async function load() {
  const crystal = scene3d();
  const [stats, data] = await Promise.all([api("GET", "/api/stats"), api("GET", "/api/reviews")]);
  reviews = data;

  card("Reviews", stats.reviews, (v) => Math.round(v));
  card("Total cost", stats.total_cost_usd, (v) => "$" + v.toFixed(4));
  card("Avg cost / review", stats.avg_cost_usd, (v) => "$" + v.toFixed(4));
  card("Avg seconds", stats.avg_duration_s, (v) => v.toFixed(1) + "s");

  Chart.defaults.color = "#8b94b3";
  Chart.defaults.borderColor = "rgba(255,255,255,.08)";
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  sevChart = new Chart($("sev"), {
    type: "bar",
    data: { labels: ["High", "Medium", "Low"],
            datasets: [{ data: [0, 0, 0], backgroundColor: ["#ff4d6d", "#ffb02e", "#7d8aa8"], borderRadius: 10, borderSkipped: false }] },
    options: { plugins: { legend: { display: false } }, animation: { duration: 1400, easing: "easeOutQuart" } },
  });
  const grad = $("cost").getContext("2d").createLinearGradient(0, 0, 0, 260);
  grad.addColorStop(0, "rgba(124,92,255,.5)"); grad.addColorStop(1, "rgba(124,92,255,0)");
  costChart = new Chart($("cost"), {
    type: "line",
    data: { labels: [], datasets: [{ data: [], borderColor: "#22d3ee", backgroundColor: grad, fill: true, tension: 0.4,
                                     pointRadius: 3, pointBackgroundColor: "#22d3ee" }] },
    options: { plugins: { legend: { display: false } }, animation: { duration: 1600 } },
  });

  ["q", "frepo", "fmodel", "fsev"].forEach((id) => $(id).addEventListener("input", render));
  await Promise.all([loadAccounts(), loadRepos()]);
  render();

  if (reduced) return;
  gsap.from("header > *", { y: 40, opacity: 0, duration: 0.9, stagger: 0.12, ease: "power3.out" });
  gsap.from(".card", { y: 60, opacity: 0, rotationX: -25, duration: 0.9, stagger: 0.1, ease: "back.out(1.4)", delay: 0.3 });
  gsap.from(".panel", { y: 50, opacity: 0, duration: 0.9, stagger: 0.15, ease: "power3.out", delay: 0.7 });
  gsap.from("#rows tr", { x: -30, opacity: 0, duration: 0.5, stagger: 0.05, ease: "power2.out", delay: 1.1 });
  gsap.from(crystal.scale, { x: 0, y: 0, z: 0, duration: 1.8, ease: "elastic.out(1,.6)", delay: 0.2 });
}

load().catch((e) => { document.body.append("Failed to load: " + e); });
