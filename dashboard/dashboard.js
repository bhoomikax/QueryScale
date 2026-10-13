"use strict";

const refreshButton = document.querySelector("#refresh-dashboard");
const renderedAt = document.querySelector("#rendered-at");
const workspaceState = document.querySelector("#workspace-state");

function setMetric(name, value) {
  const element = document.querySelector(`[data-metric="${name}"]`);
  if (element) {
    element.textContent = value;
  }
}

function showListEmpty(container, title, detail) {
  container.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  const mark = document.createElement("span");
  mark.className = "empty-state-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = "+";
  const heading = document.createElement("p");
  heading.textContent = title;
  const explanation = document.createElement("span");
  explanation.textContent = detail;
  empty.append(mark, heading, explanation);
  container.appendChild(empty);
}

function renderRecommendations(recommendations) {
  const container = document.querySelector("#recommendation-list");
  if (!container) {
    return;
  }
  if (!Array.isArray(recommendations) || recommendations.length === 0) {
    showListEmpty(container, "No recommendations loaded", "Run the analysis pipeline to populate this view.");
    return;
  }

  container.replaceChildren();
  recommendations.slice(0, 6).forEach((recommendation) => {
    const item = document.createElement("article");
    item.className = "recommendation-item";

    const heading = document.createElement("div");
    heading.className = "recommendation-heading";
    const candidate = document.createElement("strong");
    candidate.textContent = recommendation.candidate_id || "Candidate";
    const priority = document.createElement("span");
    priority.className = `priority priority-${String(recommendation.priority || "").toLowerCase()}`;
    priority.textContent = recommendation.priority || "Unranked";
    heading.append(candidate, priority);

    const target = document.createElement("p");
    target.className = "recommendation-target";
    target.textContent = `${recommendation.table || "Unknown table"} / ${(recommendation.columns || []).join(", ")}`;

    const reason = document.createElement("p");
    reason.className = "recommendation-reason";
    reason.textContent = recommendation.reason || "No reason provided.";

    const score = document.createElement("span");
    score.className = "recommendation-score";
    score.textContent = `Score ${Number(recommendation.score || 0).toFixed(1)}`;
    item.append(heading, target, reason, score);
    container.appendChild(item);
  });
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (response.status === 404) {
    return [];
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${url}`);
  }
  return response.json();
}

function updateSummary(queries, slowQueries, recommendations, benchmarkResults) {
  setMetric("queries", queries.length);
  setMetric("slow", slowQueries.length);
  setMetric("recommendations", recommendations.length);
  setMetric(
    "high-priority",
    recommendations.filter((recommendation) => String(recommendation.priority).toLowerCase() === "high").length,
  );

  if (!benchmarkResults.length) {
    setMetric("before", "--");
    setMetric("after", "--");
    setMetric("speedup", "--");
    setMetric("improvement", "--");
    return;
  }

  const executionCount = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.execution_count) || 0),
    0,
  );
  if (!executionCount) {
    setMetric("before", "--");
    setMetric("after", "--");
    setMetric("speedup", "--");
    setMetric("improvement", "--");
    return;
  }
  const before = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.before_avg_ms) || 0) * (Number(row.execution_count) || 0),
    0,
  ) / executionCount;
  const after = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.after_avg_ms) || 0) * (Number(row.execution_count) || 0),
    0,
  ) / executionCount;
  setMetric("before", before.toFixed(1));
  setMetric("after", after.toFixed(1));
  setMetric("speedup", after ? `${(before / after).toFixed(2)}x` : "--");
  setMetric("improvement", before ? `${(((before - after) / before) * 100).toFixed(1)}%` : "--");
}

function showChartEmpty(container, message) {
  if (!container) {
    return;
  }
  container.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "chart-empty";
  empty.textContent = message;
  container.appendChild(empty);
}

function makeBarLine(label, value, maximum, fillClass, suffix) {
  const line = document.createElement("div");
  line.className = "bar-line";

  const name = document.createElement("span");
  name.className = "bar-name";
  name.textContent = label;

  const track = document.createElement("span");
  track.className = "bar-track";
  const fill = document.createElement("span");
  fill.className = `bar-fill ${fillClass}`;
  fill.style.width = `${Math.max(0, Math.min(100, (value / maximum) * 100))}%`;
  track.appendChild(fill);

  const formattedValue = document.createElement("span");
  formattedValue.className = "bar-value";
  formattedValue.textContent = `${value.toFixed(1)}${suffix}`;

  line.append(name, track, formattedValue);
  return line;
}

function makeChartRow(label) {
  const row = document.createElement("div");
  row.className = "chart-row";
  const rowLabel = document.createElement("span");
  rowLabel.className = "chart-row-label";
  rowLabel.textContent = label;
  row.appendChild(rowLabel);
  return row;
}

function renderPerformanceChart(results) {
  const container = document.querySelector("#performance-chart");
  if (!Array.isArray(results) || results.length === 0) {
    showChartEmpty(container, "Benchmark data will appear here.");
    return;
  }

  const rows = results.slice(0, 6);
  const maximum = Math.max(...rows.map((row) => Number(row.before_avg_ms) || 0), 1);
  container.replaceChildren();
  rows.forEach((result) => {
    const row = makeChartRow(result.query_id || "Query");
    row.append(
      makeBarLine("Before", Number(result.before_avg_ms) || 0, maximum, "bar-fill-before", " ms"),
      makeBarLine("After", Number(result.after_avg_ms) || 0, maximum, "bar-fill-after", " ms"),
    );
    container.appendChild(row);
  });
}

function renderRecommendationChart(recommendations) {
  const container = document.querySelector("#recommendation-chart");
  if (!Array.isArray(recommendations) || recommendations.length === 0) {
    showChartEmpty(container, "Recommendation scores will appear here.");
    return;
  }

  const rows = recommendations.slice(0, 6);
  const maximum = Math.max(...rows.map((row) => Number(row.score) || 0), 1);
  container.replaceChildren();
  rows.forEach((recommendation) => {
    const row = makeChartRow(recommendation.candidate_id || "Candidate");
    row.append(makeBarLine("Score", Number(recommendation.score) || 0, maximum, "bar-fill-score", ""));
    container.appendChild(row);
  });
}

function renderFrequencyChart(queries) {
  const container = document.querySelector("#frequency-chart");
  if (!Array.isArray(queries) || queries.length === 0) {
    showChartEmpty(container, "Workload frequency will appear here.");
    return;
  }

  const grouped = new Map();
  queries.forEach((query) => {
    const current = grouped.get(query.query_id) || { count: 0, totalMs: 0 };
    current.count += 1;
    current.totalMs += Number(query.execution_time_ms) || 0;
    grouped.set(query.query_id, current);
  });
  const rows = [...grouped.entries()].sort((left, right) => right[1].count - left[1].count).slice(0, 8);
  const maximum = Math.max(...rows.map(([, value]) => value.count), 1);
  container.replaceChildren();
  rows.forEach(([queryId, value]) => {
    const row = makeChartRow(queryId || "Query");
    row.append(makeBarLine("Runs", value.count, maximum, "bar-fill-frequency", ""));
    const average = document.createElement("span");
    average.className = "chart-row-value";
    average.textContent = `Average latency ${ (value.totalMs / value.count).toFixed(1) } ms`;
    row.appendChild(average);
    container.appendChild(row);
  });
}

async function loadDashboardData() {
  try {
    const [queries, slowQueries, recommendations, benchmarkResults] = await Promise.all([
      fetchJson("/api/queries"),
      fetchJson("/api/queries/slow"),
      fetchJson("/api/recommendations"),
      fetchJson("/api/benchmark/results"),
    ]);
    updateSummary(queries, slowQueries, recommendations, benchmarkResults);
    renderRecommendations(recommendations);
    renderPerformanceChart(benchmarkResults);
    renderRecommendationChart(recommendations);
    renderFrequencyChart(queries);
    if (workspaceState) {
      workspaceState.textContent = queries.length || recommendations.length || benchmarkResults.length
        ? "Live data"
        : "Awaiting artifacts";
    }
  } catch (error) {
    setMetric("queries", "--");
    setMetric("slow", "--");
    setMetric("recommendations", "--");
    setMetric("high-priority", "--");
    showListEmpty(document.querySelector("#recommendation-list"), "API unavailable", "Start the local application to load dashboard data.");
    showChartEmpty(document.querySelector("#performance-chart"), "API unavailable.");
    showChartEmpty(document.querySelector("#recommendation-chart"), "API unavailable.");
    showChartEmpty(document.querySelector("#frequency-chart"), "API unavailable.");
    if (workspaceState) {
      workspaceState.textContent = "API unavailable";
    }
    console.error(error);
  }
  if (renderedAt) {
    renderedAt.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }
}

showChartEmpty(document.querySelector("#performance-chart"), "Benchmark data will appear here.");
showChartEmpty(document.querySelector("#recommendation-chart"), "Recommendation scores will appear here.");
showChartEmpty(document.querySelector("#frequency-chart"), "Workload frequency will appear here.");

window.QueryScaleDashboard = Object.freeze({
  loadDashboardData,
  renderPerformanceChart,
  renderRecommendationChart,
  renderFrequencyChart,
});

if (refreshButton) {
  refreshButton.addEventListener("click", () => window.location.reload());
}

if (renderedAt) {
  renderedAt.textContent = "Loading dashboard";
}

loadDashboardData();