"use strict";

const el = (id) => document.getElementById(id);
const SERVICE_CATALOG = {
  checkup: { title: "General check-up", detail: "Routine consultation · 30 min", icon: "✚" },
  dental: { title: "Dental visit", detail: "Oral health review · 30 min", icon: "◇" },
  eye: { title: "Eye test", detail: "Vision screening · 30 min", icon: "◎" },
};
const selection = { service: "checkup", date: "", time: "" };
let activeCode = null;
let latestSlotRequest = 0;
let staffDay = "today";
let latestStaffRequest = 0;
let toastTimeout;

function indiaToday() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const part = (name) => parts.find((item) => item.type === name).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}
function addDays(isoDate, count) {
  const dt = new Date(`${isoDate}T00:00:00Z`);
  dt.setUTCDate(dt.getUTCDate() + count);
  return dt.toISOString().slice(0, 10);
}
function dayDate(isoDate) { return new Date(`${isoDate}T12:00:00Z`); }
function isWeekend(isoDate) { return [0, 6].includes(dayDate(isoDate).getUTCDay()); }
function friendlyDate(isoDate, opts = { weekday: "short", day: "numeric", month: "short", year: "numeric" }) {
  return dayDate(isoDate).toLocaleDateString("en-IN", { ...opts, timeZone: "UTC" });
}
function make(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
async function request(path, options) {
  try {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    return { status: response.status, data: body };
  } catch (error) {
    return { status: 0, data: { error: "Cannot connect to the application server." } };
  }
}
function toast(message, error = false) {
  const item = el("toast");
  item.textContent = message;
  item.className = error ? "toast error" : "toast";
  item.hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => { item.hidden = true; }, 5100);
}
function openSection(name) {
  ["book", "find", "staff"].forEach((section) => {
    el(section).hidden = section !== name;
    const button = el(`nav-${section}`);
    button.classList.toggle("active", section === name);
    if (section === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (name === "staff") loadStaff();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function renderServices() {
  const parent = el("services"); parent.replaceChildren();
  for (const [key, svc] of Object.entries(SERVICE_CATALOG)) {
    const button = make("button", "service-option");
    button.type = "button";
    button.setAttribute("aria-pressed", String(selection.service === key));
    button.append(make("span", "service-icon", svc.icon), make("strong", "", svc.title), make("small", "", svc.detail));
    button.addEventListener("click", () => { selection.service = key; renderServices(); updateSummary(); });
    parent.append(button);
  }
}
function renderCalendar() {
  const parent = el("quick-days"); parent.replaceChildren();
  const start = indiaToday();
  for (let i = 0; i < 14; i += 1) {
    const day = addDays(start, i);
    const dt = dayDate(day);
    const button = make("button", "day-button");
    button.type = "button";
    button.disabled = isWeekend(day);
    button.title = button.disabled ? "Clinic closed on weekends" : friendlyDate(day);
    button.setAttribute("aria-pressed", String(day === selection.date));
    button.append(
      make("small", "", i === 0 ? "TODAY" : friendlyDate(day, { weekday: "short" }).toUpperCase()),
      make("b", "", String(dt.getUTCDate())),
      make("small", "month", friendlyDate(day, { month: "short" })),
    );
    button.addEventListener("click", () => chooseDay(day));
    parent.append(button);
  }
}
function chooseDay(date) {
  selection.date = date; selection.time = "";
  el("date").value = date;
  renderCalendar(); updateSummary(); loadSlots();
}
async function loadSlots() {
  const requestId = ++latestSlotRequest;
  selection.time = ""; updateSummary();
  const parent = el("slots"), message = el("slot-message");
  parent.replaceChildren();
  message.textContent = "Checking available appointments…";
  message.className = "notice";
  if (!selection.date) { message.textContent = "Choose a date to continue."; return; }
  if (isWeekend(selection.date)) {
    message.textContent = "The clinic is closed on Saturday and Sunday. Choose a weekday.";
    return;
  }
  const result = await request(`/api/slots?date=${encodeURIComponent(selection.date)}`);
  if (requestId !== latestSlotRequest) return; // Don't show stale results after fast clicks.
  if (result.status !== 200) {
    message.textContent = result.data.error || "Unable to load appointments.";
    message.className = "notice error"; return;
  }
  if (result.data.closed) { message.textContent = result.data.reason || "The clinic is closed on this date."; return; }
  const slots = result.data.slots || [];
  if (!slots.length) { message.textContent = "No slots have been published for this date."; return; }
  message.textContent = slots.some((item) => item.free) ? "Select one available time to continue." : "All appointments on this date are taken or have passed.";
  for (const [label, include] of [
    ["MORNING", (time) => time < "12:00"],
    ["AFTERNOON", (time) => time >= "12:00"],
  ]) {
    const group = slots.filter((slot) => include(slot.time));
    if (!group.length) continue;
    const section = make("div", "slot-group");
    section.append(make("div", "slot-group-title", label));
    const grid = make("div", "slot-grid");
    for (const slot of group) {
      const button = make("button", "time-slot", slot.time);
      button.type = "button";
      button.disabled = !slot.free;
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", `${slot.time} ${slot.free ? "available" : "unavailable"}`);
      button.addEventListener("click", () => {
        selection.time = slot.time;
        parent.querySelectorAll(".time-slot").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        updateSummary();
      });
      grid.append(button);
    }
    section.append(grid); parent.append(section);
  }
}
function updateSummary() {
  el("summary-service").textContent = SERVICE_CATALOG[selection.service].title;
  el("summary-date").textContent = selection.date ? friendlyDate(selection.date) : "Choose a day";
  el("summary-time").textContent = selection.time ? `${selection.time} IST` : "Choose a time";
  const name = el("name").value.trim(), email = el("email").value.trim();
  el("summary-name").textContent = name || "Your name";
  el("book-button").disabled = !(
    selection.time && name.length >= 2 && name.length <= 60 &&
    email.length <= 120 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
  );
}
async function bookAppointment() {
  const button = el("book-button"); button.disabled = true; button.textContent = "Confirming…";
  const result = await request("/api/bookings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      date: selection.date, time: selection.time, service: selection.service,
      name: el("name").value.trim(), email: el("email").value.trim(),
    }),
  });
  button.textContent = "Confirm appointment →";
  if (result.status === 201) {
    el("booking-flow").hidden = true;
    el("booking-success").hidden = false;
    el("success-code").textContent = result.data.code;
    el("success-summary").textContent = `${result.data.service} · ${friendlyDate(result.data.date)} · ${result.data.time} IST`;
    el("booking-success").scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    toast(result.data.error || "Could not book your appointment.", true);
    if (result.status === 409) loadSlots();
    updateSummary();
  }
}
async function lookupAppointment() {
  const code = el("booking-code").value.trim().toUpperCase();
  const message = el("find-message"); el("booking-found").hidden = true; message.hidden = true;
  if (!/^[0-9A-F]{10}$/.test(code)) {
    message.hidden = false; message.textContent = "Enter your valid 10-character reference.";
    message.className = "notice error"; return;
  }
  const result = await request(`/api/bookings/${encodeURIComponent(code)}`);
  if (result.status !== 200) {
    message.hidden = false; message.className = "notice error";
    message.textContent = result.data.error || "Unable to look up the booking."; return;
  }
  activeCode = code;
  const row = result.data;
  el("found-name").textContent = row.name;
  el("found-service").textContent = row.service;
  el("found-date").textContent = friendlyDate(row.date);
  el("found-time").textContent = `${row.time} IST`;
  el("found-status").textContent = row.status === "upcoming" ? "Upcoming" : "Past";
  el("found-status").className = row.status === "upcoming" ? "status-pill" : "status-pill past";
  el("cancel-button").hidden = row.status !== "upcoming";
  el("booking-found").hidden = false;
}
async function cancelAppointment() {
  if (!activeCode || !window.confirm("Cancel this appointment? This cannot be undone.")) return;
  const result = await request(`/api/bookings/${encodeURIComponent(activeCode)}`, { method: "DELETE" });
  if (result.status === 200) {
    activeCode = null; el("booking-found").hidden = true;
    toast("Appointment cancelled. An e-mail notification will be attempted.");
    loadSlots();
  } else toast(result.data.error || "Unable to cancel the booking.", true);
}
function chooseStaffDay(day) {
  if (day !== "today" && day !== "tomorrow") return;
  staffDay = day;
  for (const option of ["today", "tomorrow"]) {
    const button = el(`staff-${option}`);
    const active = option === day;
    button.setAttribute("aria-pressed", String(active));
    button.classList.toggle("primary", active);
    button.classList.toggle("outline", !active);
  }
  const label = day === "today" ? "Today's" : "Tomorrow's";
  el("staff-title").textContent = `${label} clinic schedule.`;
  el("staff-table-title").textContent = `${label} schedule`;
  el("staff-count-label").textContent = `Appointments ${day}`;
  loadStaff();
}
async function loadStaff() {
  const requestId = ++latestStaffRequest;
  const body = el("staff-rows"), message = el("staff-message");
  const requestedDay = staffDay;
  const date = requestedDay === "tomorrow" ? addDays(indiaToday(), 1) : indiaToday();
  el("staff-date").textContent = friendlyDate(date);
  body.replaceChildren();
  message.textContent = `Loading ${requestedDay}'s schedule…`;
  message.className = "notice";
  el("staff-count").textContent = "—";
  el("staff-first").textContent = "—";

  // Reuse the existing appointment-availability API for tomorrow.
  // All personal information stays on the backend.
  const path = requestedDay === "today"
    ? "/api/staff/today"
    : `/api/slots?date=${encodeURIComponent(date)}`;
  const result = await request(path, { cache: "no-store" });
  if (requestId !== latestStaffRequest) return; // Ignore stale responses after switching days.
  if (result.status !== 200) {
    message.textContent = result.data.error || "Could not load the schedule.";
    message.className = "notice error";
    return;
  }

  let schedule = result.data;
  if (requestedDay === "tomorrow") {
    const slots = Array.isArray(result.data.slots) ? result.data.slots : [];
    const booked = slots.filter((slot) => !slot.free);
    schedule = {
      closed: result.data.closed,
      reason: result.data.reason,
      count: booked.length,
      first_appointment: booked.length ? booked[0].time : null,
      bookings: slots.map((slot) => ({
        time: slot.time,
        status: slot.free ? "Available" : "Booked",
      })),
    };
  }

  el("staff-count").textContent = String(schedule.count);
  el("staff-first").textContent = schedule.first_appointment || "—";
  if (schedule.closed) {
    message.textContent = schedule.reason || "The clinic is closed on this date.";
    return;
  }
  if (!schedule.bookings.length) {
    message.textContent = "No appointment slots are available on this date.";
    return;
  }
  message.textContent = "Times shown in IST. Patient details are private.";
  for (const slot of schedule.bookings) {
    const tr = make("tr");
    for (const value of [slot.time, slot.status]) tr.append(make("td", "", String(value)));
    body.append(tr);
  }
}
async function revealLocalDebug() {
  try {
    const response = await fetch("/debug/state");
    if (!response.ok) return;
    const content = await response.json();
    if (Array.isArray(content.timeline) && Array.isArray(content.items)) el("developer-link").hidden = false;
  } catch { /* AWS does not expose the developer inspector */ }
}
function init() {
  const today = indiaToday();
  let first = today;
  while (isWeekend(first)) first = addDays(first, 1);
  el("date").min = today;
  el("date").max = addDays(today, 60);
  el("date").addEventListener("change", () => { if (el("date").value) chooseDay(el("date").value); });
  el("name").addEventListener("input", updateSummary);
  el("email").addEventListener("input", updateSummary);
  el("book-button").addEventListener("click", bookAppointment);
  el("book-again").addEventListener("click", () => { el("booking-flow").hidden = false; el("booking-success").hidden = true; chooseDay(first); });
  el("copy-code").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(el("success-code").textContent); toast("Reference copied."); }
    catch { toast("Copy manually: " + el("success-code").textContent); }
  });
  el("find-button").addEventListener("click", lookupAppointment);
  el("cancel-button").addEventListener("click", cancelAppointment);
  el("staff-refresh").addEventListener("click", loadStaff);
  el("staff-today").addEventListener("click", () => chooseStaffDay("today"));
  el("staff-tomorrow").addEventListener("click", () => chooseStaffDay("tomorrow"));
  el("nav-book").addEventListener("click", () => openSection("book"));
  el("nav-find").addEventListener("click", () => openSection("find"));
  el("nav-staff").addEventListener("click", () => openSection("staff"));
  renderServices(); chooseDay(first); revealLocalDebug();
}
init();
