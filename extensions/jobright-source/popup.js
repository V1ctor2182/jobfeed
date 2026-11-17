const statusElement = document.querySelector("#status");
const baseUrlInput = document.querySelector("#base-url");
const saveButton = document.querySelector("#save");

function refresh() {
  chrome.runtime.sendMessage({ type: "status" }, (response) => {
    if (chrome.runtime.lastError || !response) {
      renderStatus("error");
      return;
    }
    baseUrlInput.value = response.baseUrl;
    renderStatus(response.state);
  });
}

function renderStatus(state) {
  statusElement.className = `status ${state}`;
  statusElement.textContent = state === "connected"
    ? "Connected to Jobfeed"
    : `Bridge ${state}`;
}

saveButton.addEventListener("click", () => {
  saveButton.disabled = true;
  chrome.runtime.sendMessage(
    { type: "configure", baseUrl: baseUrlInput.value },
    (response) => {
      saveButton.disabled = false;
      if (chrome.runtime.lastError || !response) {
        renderStatus("error");
        return;
      }
      baseUrlInput.value = response.baseUrl;
      renderStatus(response.state);
      setTimeout(refresh, 500);
    },
  );
});

refresh();

const applicationButton = document.querySelector("#application-connect");
const applicationCode = document.querySelector("#application-code");
const applicationStatus = document.querySelector("#application-status");
applicationButton.addEventListener("click", () => {
  applicationButton.disabled = true;
  applicationStatus.textContent = "Connecting this tab…";
  chrome.runtime.sendMessage({ type: "application_tab_connect",
    baseUrl: document.querySelector("#application-dashboard").value, code: applicationCode.value }, response => {
    applicationButton.disabled = false;
    if (chrome.runtime.lastError || !response) {
      applicationStatus.textContent = "The extension could not connect. Try again.";
      return;
    }
    if (response.error) { applicationStatus.textContent = response.error; return; }
    applicationCode.value = "";
    applicationStatus.textContent = "Tab connected. Use Jump to Chrome tab in Runs.";
  });
});
