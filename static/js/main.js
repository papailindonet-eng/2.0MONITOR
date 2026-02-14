const socket = io();

const filaCountEl = document.getElementById("fila-count");
const vehiclesTableBody = document.getElementById("vehicles-table-body");
const historyTableBody = document.getElementById("history-table-body");
const chamadosTableBody = document.getElementById("chamados-table-body");
const chamadosSearch = document.getElementById("chamados-search");
const chamadosStatusFilter = document.getElementById("chamados-status-filter");
const chamadosTransportadoraFilter = document.getElementById(
  "chamados-transportadora-filter",
);
const chamadosDateFilter = document.getElementById("chamados-date-filter");

const alertModal = document.getElementById("alert-modal");
const alertDetails = document.getElementById("alert-details");
const alertConfirmButton = document.getElementById("alert-confirm-button");
const alertAudio = document.getElementById("alert-audio");

let currentAlertId = null;

function formatTimestamp(timestamp) {
  if (!timestamp) return "-";
  return new Date(timestamp).toLocaleString();
}

function renderVehicles(rows, target) {
  if (!target) return;
  target.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-700";
    tr.innerHTML = `
      <td class="px-4 py-2">${row.placa}</td>
      <td class="px-4 py-2">${row.transportadora}</td>
      <td class="px-4 py-2">${row.status_atual}</td>
      <td class="px-4 py-2">${formatTimestamp(
        row.timestamp_ultima_atualizacao,
      )}</td>
    `;
    target.appendChild(tr);
  });
}

function renderHistory(rows) {
  if (!historyTableBody) return;
  historyTableBody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-700";
    tr.innerHTML = `
      <td class="px-4 py-2">${row.placa}</td>
      <td class="px-4 py-2">${row.transportadora}</td>
      <td class="px-4 py-2">${row.status_anterior || "-"}</td>
      <td class="px-4 py-2">${row.status_novo}</td>
      <td class="px-4 py-2">${formatTimestamp(row.timestamp_mudanca)}</td>
      <td class="px-4 py-2">${row.usuario_confirmacao || "-"}</td>
      <td class="px-4 py-2">${formatTimestamp(
        row.timestamp_confirmacao,
      )}</td>
    `;
    historyTableBody.appendChild(tr);
  });
}

function updateFiltroTransportadora(rows) {
  if (!chamadosTransportadoraFilter) return;
  const transportadoras = new Set(rows.map((row) => row.transportadora));
  chamadosTransportadoraFilter.innerHTML = '<option value="">Todas</option>';
  transportadoras.forEach((item) => {
    const option = document.createElement("option");
    option.value = item;
    option.textContent = item;
    chamadosTransportadoraFilter.appendChild(option);
  });
}

function filterChamados(rows) {
  let filtered = rows;
  const searchValue = chamadosSearch?.value?.toLowerCase() || "";
  const statusValue = chamadosStatusFilter?.value || "";
  const transportadoraValue = chamadosTransportadoraFilter?.value || "";
  const dateValue = chamadosDateFilter?.value || "";

  if (searchValue) {
    filtered = filtered.filter(
      (row) =>
        row.placa.toLowerCase().includes(searchValue) ||
        row.transportadora.toLowerCase().includes(searchValue),
    );
  }
  if (statusValue) {
    filtered = filtered.filter((row) => row.status_atual === statusValue);
  }
  if (transportadoraValue) {
    filtered = filtered.filter(
      (row) => row.transportadora === transportadoraValue,
    );
  }
  if (dateValue) {
    filtered = filtered.filter((row) =>
      row.timestamp_ultima_atualizacao.startsWith(dateValue),
    );
  }
  return filtered;
}

function renderChamados(rows) {
  if (!chamadosTableBody) return;
  const filtered = filterChamados(rows);
  chamadosTableBody.innerHTML = "";
  filtered.forEach((row) => {
    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-700";
    tr.innerHTML = `
      <td class="px-4 py-2">${row.placa}</td>
      <td class="px-4 py-2">${row.transportadora}</td>
      <td class="px-4 py-2">${row.status_atual}</td>
      <td class="px-4 py-2">${formatTimestamp(
        row.timestamp_ultima_atualizacao,
      )}</td>
    `;
    chamadosTableBody.appendChild(tr);
  });
}

let latestChamados = [];

function refreshChamados() {
  if (!chamadosTableBody) return;
  fetch("/api/chamados_portaria")
    .then((response) => response.json())
    .then((data) => {
      latestChamados = data;
      updateFiltroTransportadora(data);
      renderChamados(data);
    });
}

if (chamadosSearch) {
  [
    chamadosSearch,
    chamadosStatusFilter,
    chamadosTransportadoraFilter,
    chamadosDateFilter,
  ].forEach((element) => {
    if (!element) return;
    element.addEventListener("input", () => renderChamados(latestChamados));
    element.addEventListener("change", () => renderChamados(latestChamados));
  });
}

socket.on("update_data", (payload) => {
  if (filaCountEl) {
    filaCountEl.textContent = payload.fila_count;
  }
  renderVehicles(payload.vehicles, vehiclesTableBody);
  renderHistory(payload.history);
});

socket.on("critical_alert", (payload) => {
  currentAlertId = payload.history_id;
  if (alertDetails) {
    alertDetails.textContent = `${payload.transportadora} - ${payload.placa} (${payload.status})`;
  }
  if (alertAudio) {
    alertAudio.volume = payload.volume ?? 1.0;
    alertAudio.loop = true;
    alertAudio.play();
  }
  if (alertModal) {
    alertModal.classList.remove("hidden");
  }
});

if (alertConfirmButton) {
  alertConfirmButton.addEventListener("click", () => {
    if (!currentAlertId) return;
    fetch("/api/confirm_alert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_id: currentAlertId }),
    }).then(() => {
      if (alertAudio) {
        alertAudio.pause();
        alertAudio.currentTime = 0;
      }
      if (alertModal) {
        alertModal.classList.add("hidden");
      }
      currentAlertId = null;
    });
  });
}

if (vehiclesTableBody) {
  fetch("/api/veiculos")
    .then((response) => response.json())
    .then((data) => renderVehicles(data, vehiclesTableBody));
}

if (historyTableBody) {
  fetch("/api/historico")
    .then((response) => response.json())
    .then((data) => renderHistory(data));
}

if (chamadosTableBody) {
  refreshChamados();
}
