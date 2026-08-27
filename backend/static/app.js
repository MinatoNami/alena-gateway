/* alena-gateway status page.
 *
 * Polls /api/status and re-renders. Everything is written with textContent
 * rather than innerHTML: the strings come from services.yaml and from upstream
 * error messages, and an httpx exception is not a place to start trusting
 * markup. */

const POLL_MS = 15000;

const list = document.getElementById('services');
const summary = document.getElementById('summary');
const originEl = document.getElementById('origin');
const gatewayMeta = document.getElementById('gateway-meta');
const refreshButton = document.getElementById('refresh');
const template = document.getElementById('service-template');

let timer = null;

function fact(dl, label, value, bad = false) {
  if (value === null || value === undefined || value === '') return;
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  dd.textContent = value;
  if (bad) dd.classList.add('bad');
  dl.append(dt, dd);
}

function renderService(service) {
  const node = template.content.cloneNode(true);
  const up = service.status === 'up';

  const status = node.querySelector('.status');
  status.classList.add(up ? 'status-up' : 'status-down');
  node.querySelector('.status-text').textContent = up ? 'Operational' : 'Not responding';

  // A routed service is a link to itself; one that is deliberately not exposed
  // through the gateway is named but not linked, so the page never offers a
  // URL that would 404.
  const heading = node.querySelector('.name');
  if (service.url) {
    const link = node.querySelector('.name-link');
    link.textContent = service.name;
    link.href = service.url;
  } else {
    const span = document.createElement('span');
    span.textContent = service.name;
    heading.replaceChildren(span);
  }

  node.querySelector('.blurb').textContent = service.blurb || '';

  const facts = node.querySelector('.facts');
  if (service.url) fact(facts, 'Path', new URL(service.url).pathname);
  fact(facts, 'Upstream', `127.0.0.1:${service.port}`);
  if (up && service.latency_ms !== null) fact(facts, 'Latency', `${service.latency_ms} ms`);
  if (!up) fact(facts, 'Error', service.detail || 'unknown', true);
  if (service.reserves && service.reserves.length) {
    fact(facts, 'Also serves', service.reserves.join('  '));
  }
  for (const component of service.components || []) {
    const componentUp = component.status === 'up';
    fact(
      facts,
      component.name,
      componentUp ? `up · :${component.port}` : `down · ${component.detail || 'unknown'}`,
      !componentUp,
    );
  }

  return node;
}

function render(data) {
  originEl.textContent = data.origin.replace(/^https?:\/\//, '');

  list.replaceChildren(...data.services.map(renderService));
  list.setAttribute('aria-busy', 'false');

  const down = data.services.filter((service) => service.status !== 'up');
  summary.textContent = down.length === 0
    ? `All ${data.services.length} services operational.`
    : `${down.length} of ${data.services.length} not responding: ${down.map((s) => s.name).join(', ')}.`;

  const checked = new Date(data.generated_at);
  gatewayMeta.textContent =
    `Gateway ${data.gateway.version} · checked ${checked.toLocaleTimeString()}`;
}

function renderFailure(error) {
  list.setAttribute('aria-busy', 'false');
  const item = document.createElement('li');
  item.className = 'loading';
  item.textContent = `Could not reach the status service: ${error.message}`;
  list.replaceChildren(item);
  summary.textContent = 'Status unavailable.';
}

async function poll() {
  refreshButton.disabled = true;
  try {
    const response = await fetch('api/status', { headers: { accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    renderFailure(error);
  } finally {
    refreshButton.disabled = false;
  }
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(poll, POLL_MS);
}

// A backgrounded tab polling every 15 seconds is pure waste, and coming back to
// a page showing minute-old state is worse than a brief spinner.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(timer);
  } else {
    poll();
    schedule();
  }
});

refreshButton.addEventListener('click', poll);

poll();
schedule();
