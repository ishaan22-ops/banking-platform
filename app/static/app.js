const authView = document.querySelector("#auth-view");
const dashboard = document.querySelector("#dashboard");
const authForm = document.querySelector("#auth-form");
const authMessage = document.querySelector("#auth-message");
const tokenKey = "harbor-demo-token";
let registering = false;
let transactionOffset = 0;
const pageSize = 20;
let searchTimer;
let depositIdempotencyKey;
let transferIdempotencyKey;

function getToken() {
  return sessionStorage.getItem(tokenKey);
}

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  if (getToken()) headers.set("Authorization", `Bearer ${getToken()}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "Something went wrong. Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function formatMoney(cents) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

function showAuth() {
  authView.classList.remove("hidden");
  dashboard.classList.add("hidden");
}

function showDashboard() {
  authView.classList.add("hidden");
  dashboard.classList.remove("hidden");
  transactionOffset = 0;
  loadAccount();
  loadTransactions(true);
}

async function loadAccount() {
  try {
    const account = await api("/api/account");
    document.querySelector("#balance").textContent = formatMoney(account.balance_cents);
    document.querySelector("#account-email").textContent = account.email;
    setMessage(document.querySelector("#account-message"), "");
  } catch (error) {
    if (error.status === 401) {
      sessionStorage.removeItem(tokenKey);
      showAuth();
    } else {
      setMessage(document.querySelector("#account-message"), error.message, true);
    }
  }
}

function renderTransaction(item) {
  const row = document.createElement("article");
  row.className = "transaction-row";
  const icon = document.createElement("span");
  icon.className = `transaction-icon ${item.kind}`;
  icon.textContent = item.kind === "credit" ? "↓" : "↑";
  icon.setAttribute("aria-hidden", "true");
  const details = document.createElement("div");
  details.className = "transaction-details";
  const title = document.createElement("strong");
  title.textContent = item.description;
  const date = document.createElement("span");
  date.textContent = new Date(item.created_at).toLocaleString();
  details.append(title, date);
  const amount = document.createElement("strong");
  amount.className = `transaction-amount ${item.kind}`;
  amount.textContent = `${item.kind === "credit" ? "+" : "−"}${formatMoney(item.amount_cents)}`;
  row.append(icon, details, amount);
  return row;
}

async function loadTransactions(reset = false) {
  const list = document.querySelector("#transaction-list");
  if (reset) {
    transactionOffset = 0;
    list.replaceChildren();
  }
  const query = document.querySelector("#transaction-search").value.trim();
  try {
    const result = await api(`/api/transactions?q=${encodeURIComponent(query)}&limit=${pageSize}&offset=${transactionOffset}`);
    if (reset && result.items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = query ? "No matching transactions." : "Your activity will appear here.";
      list.append(empty);
    } else {
      result.items.forEach((item) => list.append(renderTransaction(item)));
    }
    transactionOffset += result.items.length;
    document.querySelector("#load-more").classList.toggle("hidden", !result.has_more);
  } catch (error) {
    if (reset) {
      const failure = document.createElement("p");
      failure.className = "empty-state error";
      failure.textContent = error.message;
      list.append(failure);
    }
  }
}

document.querySelector("#toggle-auth").addEventListener("click", (event) => {
  registering = !registering;
  document.querySelector("#auth-heading").textContent = registering ? "Create your account" : "Sign in to your account";
  document.querySelector("#auth-submit").innerHTML = registering ? "Create account <span aria-hidden=\"true\">→</span>" : "Sign in <span aria-hidden=\"true\">→</span>";
  document.querySelector("#password").autocomplete = registering ? "new-password" : "current-password";
  event.currentTarget.textContent = registering ? "Sign in instead" : "Create an account";
  authMessage.textContent = "";
});

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#auth-submit");
  button.disabled = true;
  setMessage(authMessage, registering ? "Creating your account..." : "Signing in...");
  const payload = {
    email: document.querySelector("#email").value,
    password: document.querySelector("#password").value,
  };
  try {
    const result = await api(`/api/auth/${registering ? "register" : "login"}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    sessionStorage.setItem(tokenKey, result.access_token);
    authForm.reset();
    setMessage(authMessage, "");
    showDashboard();
  } catch (error) {
    setMessage(authMessage, error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    sessionStorage.removeItem(tokenKey);
    showAuth();
  }
});

document.querySelector("#add-demo-funds").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const message = document.querySelector("#demo-funds-message");
  button.disabled = true;
  depositIdempotencyKey ||= crypto.randomUUID();
  setMessage(message, "Adding simulated funds...");
  try {
    await api("/api/demo-deposits", {
      method: "POST",
      headers: { "Idempotency-Key": depositIdempotencyKey },
    });
    depositIdempotencyKey = null;
    setMessage(message, "$500 in simulated funds added.");
    await loadAccount();
    await loadTransactions(true);
  } catch (error) {
    if ([400, 409, 422].includes(error.status)) depositIdempotencyKey = null;
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#transfer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const message = document.querySelector("#transfer-message");
  button.disabled = true;
  transferIdempotencyKey ||= crypto.randomUUID();
  setMessage(message, "Processing demo transfer...");
  const payload = {
    recipient_email: document.querySelector("#recipient").value,
    amount: document.querySelector("#amount").value,
    description: document.querySelector("#description").value || "Demo transfer",
  };
  try {
    await api("/api/transfers", {
      method: "POST",
      headers: { "Idempotency-Key": transferIdempotencyKey },
      body: JSON.stringify(payload),
    });
    transferIdempotencyKey = null;
    event.currentTarget.reset();
    document.querySelector("#description").value = "Demo transfer";
    setMessage(message, "Demo transfer completed.");
    await loadAccount();
    await loadTransactions(true);
  } catch (error) {
    if ([400, 409, 422].includes(error.status)) transferIdempotencyKey = null;
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#transaction-search").addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => loadTransactions(true), 250);
});

document.querySelector("#load-more").addEventListener("click", () => loadTransactions());

document.querySelector("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const answerElement = document.querySelector("#chat-answer");
  const questionInput = document.querySelector("#chat-question");
  button.disabled = true;
  setMessage(answerElement, "Thinking...");
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question: questionInput.value }),
    });
    setMessage(answerElement, result.answer);
    questionInput.value = "";
  } catch (error) {
    setMessage(answerElement, error.message, true);
  } finally {
    button.disabled = false;
  }
});

if (getToken()) showDashboard();
