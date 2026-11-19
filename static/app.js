const STATUS_LABELS = Object.freeze({
  queued: "Queued",
  downloading: "Downloading",
  processing: "Processing",
  done: "Completed",
  error: "Failed",
  skipped: "Already downloaded",
  unknown: "Unknown status",
});

const ACTIVE_STATUSES = new Set(["queued", "downloading", "processing"]);

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function clampPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(Math.min(100, Math.max(0, number)) * 10) / 10 : 0;
}

export function normalizeJob(job) {
  if (!job || typeof job !== "object" || Array.isArray(job)) {
    throw new ApiError("The app returned an invalid download entry.");
  }
  const text = (value, fallback = "") => typeof value === "string" ? value.slice(0, 2000) : fallback;
  const status = Object.hasOwn(STATUS_LABELS, job.status) ? job.status : "unknown";
  return {
    title: text(job.title, "Untitled download") || "Untitled download",
    status,
    label: STATUS_LABELS[status],
    percent: clampPercent(job.percent),
    format: job.format === "audio" ? "MP3 audio" : job.format === "video" ? "MP4 video" : "Download",
    speed: text(job.speed),
    eta: text(job.eta),
    message: text(job.message),
    error: text(job.error),
  };
}

export function createApiClient({ csrfToken, fetchImpl = globalThis.fetch, timeoutMs = 15000 }) {
  return async function request(path, { method = "GET", body } = {}) {
    if (!csrfToken) {
      throw new ApiError("Your page session is missing. Reload this page and try again.");
    }
    if (!path.startsWith("/api/")) {
      throw new ApiError("Only this app's API can be requested.");
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = { Accept: "application/json", "X-CSRF-Token": csrfToken };
      const options = {
        method,
        headers,
        credentials: "same-origin",
        mode: "same-origin",
        redirect: "error",
        cache: "no-store",
        signal: controller.signal,
      };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const response = await fetchImpl(path, options);
      let data;
      try {
        data = await response.json();
      } catch {
        throw new ApiError(
          response.ok ? "The app returned an unreadable response. Reload this page and try again."
            : `Request failed (HTTP ${response.status}).`,
          response.status,
        );
      }
      const error = data && typeof data.error === "string" ? data.error.slice(0, 2000) : "";
      if (!response.ok || error) {
        const detail = error || "The app could not complete this request.";
        throw new ApiError(`HTTP ${response.status}: ${detail}`, response.status);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (controller.signal.aborted) {
        throw new ApiError("The local app did not respond in time. Try again.");
      }
      throw new ApiError("Cannot reach the local app. Check that it is running and try again.");
    } finally {
      clearTimeout(timeout);
    }
  };
}

export function requireConfirmation(data) {
  if (!data || data.ok !== true) {
    throw new ApiError("The app did not confirm this action. Its result is unknown.");
  }
}

export function queuedMessage(data) {
  if (!data || !Number.isInteger(data.count) || data.count < 0 || !Array.isArray(data.queued)) {
    throw new ApiError("The app did not confirm which downloads were added.");
  }
  const duplicateCount = Array.isArray(data.duplicates) ? data.duplicates.length : 0;
  if (data.count === 0 && duplicateCount) {
    return `${duplicateCount === 1 ? "This link is" : `These ${duplicateCount} links are`} already queued. No new jobs were added.`;
  }
  const added = `Added ${data.count} ${data.count === 1 ? "link" : "links"} to the queue.`;
  const duplicates = duplicateCount
    ? ` ${duplicateCount} ${duplicateCount === 1 ? "link was" : "links were"} already queued and skipped.`
    : "";
  return added + duplicates;
}

export function createPoller({ task, onError, delay = 1000, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let stopped = true;
  let timer = null;
  let inFlight = null;

  function refresh() {
    if (stopped) return Promise.resolve();
    if (inFlight) return inFlight;
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    // Deferring task invocation also handles synchronous failures without
    // leaving the poller permanently marked as in flight.
    inFlight = Promise.resolve().then(task).catch(onError).finally(() => {
      inFlight = null;
      if (!stopped) timer = setTimer(refresh, delay);
    });
    return inFlight;
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      void refresh();
    },
    refresh,
    stop() {
      stopped = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}

function createElement(document, tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderJob(document, job) {
  const article = createElement(document, "article", "job");
  const heading = createElement(document, "div", "job-heading");
  heading.append(
    createElement(document, "h3", "job-title", job.title),
    createElement(document, "span", `badge badge-${job.status}`, job.label),
  );
  article.append(heading);
  const meta = [job.format];
  if (job.status === "downloading") {
    if (job.speed) meta.push(job.speed);
    if (job.eta) meta.push(`${job.eta} remaining`);
  }
  article.append(createElement(document, "p", "job-meta", meta.join(" · ")));
  if (job.status === "downloading" || job.status === "processing") {
    const progressRow = createElement(document, "div", "job-progress");
    const progress = createElement(document, "progress");
    progress.max = 100;
    progress.setAttribute("aria-label", `${job.label}: ${job.title}`);
    let progressText = "Converting / merging";
    if (job.status === "downloading") {
      progress.value = job.percent;
      progressText = `${job.percent}%`;
    }
    progressRow.append(progress, createElement(document, "span", "progress-value", progressText));
    article.append(progressRow);
  }
  if (job.message) article.append(createElement(document, "p", "job-message", job.message));
  if (job.error) article.append(createElement(document, "p", "job-message job-error", `Error: ${job.error}`));
  return article;
}

export function initializeApp(document, window) {
  const find = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const request = createApiClient({ csrfToken: token });
  const form = find("download-form");
  const urls = find("urls");
  const quality = find("quality");
  const downloadButton = find("download-button");
  const dialog = find("shutdown-dialog");
  const shutdownButton = find("confirm-shutdown");
  const cancelShutdown = find("cancel-shutdown");
  let format = "audio";
  let closed = false;
  let shutdownPending = false;

  function notify(message, isError = false) {
    const notice = find("message");
    notice.classList.toggle("is-error", isError);
    notice.textContent = message;
    notice.hidden = false;
  }

  function connection(message, isError = false) {
    if (closed) return;
    const status = find("connection-status");
    status.classList.toggle("is-error", isError);
    if (status.textContent !== message) status.textContent = message;
  }

  function showJobs(data) {
    if (!Array.isArray(data)) throw new ApiError("The app returned an invalid download list.");
    const jobs = data.map(normalizeJob);
    if (closed) return;
    const list = find("jobs");
    const fragment = document.createDocumentFragment();
    if (jobs.length === 0) {
      const empty = createElement(document, "div", "empty-state");
      const icon = createElement(document, "div", "empty-icon", "↓");
      icon.setAttribute("aria-hidden", "true");
      empty.append(icon, createElement(document, "h3", "", "Ready when you are."),
        createElement(document, "p", "", "Add a link above to start your first download."));
      fragment.append(empty);
    } else {
      for (const job of jobs) fragment.append(renderJob(document, job));
    }
    list.replaceChildren(fragment);
    find("job-count").textContent = String(jobs.length);
    const active = jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length;
    const failed = jobs.filter((job) => job.status === "error").length;
    const summary = jobs.length === 0 ? "Your queue is empty."
      : `${active} active · ${jobs.length - active} finished${failed ? ` · ${failed} failed` : ""}`;
    if (find("queue-summary").textContent !== summary) find("queue-summary").textContent = summary;
  }

  const poller = createPoller({
    task: async () => {
      showJobs(await request("/api/jobs"));
      connection("Connected to the local app. Updates every second.");
    },
    onError: (error) => connection(`Could not refresh downloads. ${error.message} Retrying…`, true),
  });

  for (const button of find("format-switch").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      format = button.dataset.format === "video" ? "video" : "audio";
      for (const option of find("format-switch").querySelectorAll("button")) {
        option.setAttribute("aria-pressed", String(option === button));
      }
      find("quality-field").hidden = format !== "audio";
      quality.disabled = format !== "audio";
      find("video-note").hidden = format !== "video";
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (downloadButton.disabled || closed) return;
    const submittedUrls = urls.value;
    const lines = submittedUrls.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!lines.length) {
      notify("Add at least one video or playlist link.", true);
      urls.focus();
      return;
    }
    if (lines.length > 20) {
      notify("Add up to 20 links at a time.", true);
      urls.focus();
      return;
    }
    downloadButton.disabled = true;
    downloadButton.textContent = "Adding links…";
    try {
      const data = await request("/api/download", {
        method: "POST", body: { urls: submittedUrls, format, quality: quality.value },
      });
      notify(queuedMessage(data));
      if (urls.value === submittedUrls) urls.value = "";
      void poller.refresh();
    } catch (error) {
      notify(`Could not add downloads. ${error.message}`, true);
    } finally {
      downloadButton.disabled = closed;
      downloadButton.textContent = "Add to downloads ↓";
    }
  });

  urls.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  async function performAction(button, path, successMessage) {
    if (button.disabled || closed) return;
    button.disabled = true;
    try {
      requireConfirmation(await request(path, { method: "POST" }));
      notify(successMessage);
      void poller.refresh();
    } catch (error) {
      notify(error.message, true);
    } finally {
      button.disabled = closed;
    }
  }

  find("clear-finished").addEventListener("click", () => {
    void performAction(find("clear-finished"), "/api/clear", "Finished jobs cleared. Saved files were kept.");
  });
  find("open-folder").addEventListener("click", () => {
    void performAction(find("open-folder"), "/api/open-folder", "The request to open your download folder was sent.");
  });

  find("shutdown").addEventListener("click", () => {
    find("shutdown-error").hidden = true;
    dialog.showModal();
  });
  cancelShutdown.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => {
    if (shutdownPending) event.preventDefault();
  });
  shutdownButton.addEventListener("click", async () => {
    if (shutdownPending || closed) return;
    shutdownPending = true;
    shutdownButton.disabled = true;
    cancelShutdown.disabled = true;
    shutdownButton.textContent = "Closing…";
    find("shutdown-error").hidden = true;
    try {
      requireConfirmation(await request("/api/shutdown", { method: "POST" }));
      closed = true;
      poller.stop();
      dialog.close();
      find("app-view").hidden = true;
      find("shutdown").hidden = true;
      find("closed-view").hidden = false;
      find("closed-view").focus();
    } catch (error) {
      find("shutdown-error").textContent = `Could not confirm shutdown. ${error.message}`;
      find("shutdown-error").hidden = false;
    } finally {
      shutdownPending = false;
      shutdownButton.disabled = false;
      cancelShutdown.disabled = false;
      shutdownButton.textContent = "Close app";
    }
  });

  if (token) {
    poller.start();
  } else {
    notify("Your page session is missing. Reload this page before continuing.", true);
    for (const button of document.querySelectorAll("button")) button.disabled = true;
    connection("Not connected: the page session is missing.", true);
  }
  window.addEventListener("pagehide", () => poller.stop(), { once: true });
  return { poller };
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  initializeApp(document, window);
}
