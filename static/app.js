// YT-DLP Studio Frontend Application Logic
//
// Flow: paste URL -> fetch metadata -> (optional) AI: extract transcript ->
// analyse chunks -> clip ranges on the timeline -> export with ffmpeg.

// ─── State ───────────────────────────────────────────────────────────────────
let videoDuration = 0;
let currentTime = 0;
let isPlaying = false;
let ytPlayer = null;
let activePlayerType = 'youtube';
let playerPollInterval = null;
let currentVideoUrl = "";
let currentVideoTitle = "";

// Timeline & drawing
let timelineZoom = 1;
let bookmarks = [];
let clipRegions = [];
let activeDrag = null;
let activeRegionIndex = null;

// AI pipeline
let aiChunks = null;      // [{start, end, importance}] from the transcript analysis
let aiTaskId = null;      // queue task id of the running analysis
let knownTasks = new Map();
let saveDirEdited = false;

// ─── Elements ────────────────────────────────────────────────────────────────
const videoUrlInput = document.getElementById('video-url');
const activeVideoTitle = document.getElementById('active-video-title');
const timeDisplay = document.getElementById('time-display');

const playPauseBtn = document.getElementById('btn-play-pause');
const prevFrameBtn = document.getElementById('btn-prev-frame');
const nextFrameBtn = document.getElementById('btn-next-frame');

const timelineZoomInput = document.getElementById('timeline-zoom');
const btnFitScreen = document.getElementById('btn-fit-screen');
const timelineScrollWrapper = document.getElementById('timeline-scroll-wrapper');
const timelineContent = document.getElementById('timeline-content');
const timelineCanvas = document.getElementById('timeline-canvas');

const regionsList = document.getElementById('regions-list');
const addRegionBtn = document.getElementById('add-region-btn');
const addBookmarkBtnAlt = document.getElementById('add-bookmark-btn-alt');
const btnRemoveSelected = document.getElementById('btn-remove-selected');
const btnClearAll = document.getElementById('btn-clear-all');
const selectAllRegions = document.getElementById('select-all-regions');
const exportSelectedBtn = document.getElementById('export-selected-btn');

const bookmarksList = document.getElementById('bookmarks-list');
const addBookmarkBtn = document.getElementById('add-bookmark-btn');
const aiGenerateCutsBtn = document.getElementById('ai-generate-cuts-btn');
const aiStatusEl = document.getElementById('ai-status');

const queueList = document.getElementById('queue-list');
const saveDirInput = document.getElementById('save-dir');
const quickDownloadBtn = document.getElementById('quick-download-btn');
const formatPresetSelect = document.getElementById('download-format');
const customFormatSelect = document.getElementById('custom-format');
const clipCutModeSelect = document.getElementById('clip-cut-mode');
const rpCutModeSelect = document.getElementById('rp-cut-mode');
const aiStatusRight = document.getElementById('auth-status');
const authLink = document.getElementById('auth-link');

const toggleSettingsBtn = document.getElementById('toggle-settings-btn');
const settingsPopover = document.getElementById('settings-popover');
const closeSettingsBtn = document.getElementById('close-settings-btn');

const metaChannel = document.getElementById('meta-channel');
const metaResolution = document.getElementById('meta-resolution');
const metaDuration = document.getElementById('meta-duration');
const metaUploadDate = document.getElementById('meta-upload-date');
const metaVideoCodec = document.getElementById('meta-video-codec');
const metaAudioCodec = document.getElementById('meta-audio-codec');
const metaFps = document.getElementById('meta-fps');
const metaSize = document.getElementById('meta-size');

const rangeStart = document.getElementById('range-start');
const rangeEnd = document.getElementById('range-end');
const rangeDuration = document.getElementById('range-duration');

const volumeSlider = document.getElementById('playback-volume-slider');
const tlVolumeSlider = document.getElementById('tl-volume');
const tlLegendText = document.getElementById('tl-legend-text');

const appConfig = (window.APP_CONFIG && typeof window.APP_CONFIG === 'object') ? window.APP_CONFIG : {};

saveDirInput.value = appConfig.defaultSaveDir || "";
if (clipCutModeSelect) clipCutModeSelect.value = appConfig.clipCutMode || 'precise';
if (rpCutModeSelect) rpCutModeSelect.value = appConfig.clipCutMode || 'precise';

if (appConfig.authEnabled) {
    if (aiStatusRight) aiStatusRight.textContent = 'signed in';
    if (authLink) {
        const link = document.createElement('a');
        link.href = '/logout';
        link.textContent = 'Sign out';
        link.className = 'auth-link';
        authLink.appendChild(link);
    }
}

// ─── DOM helpers (textContent only -- no HTML injection anywhere) ─────────────
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function iconButton(label, className, onClick, title) {
    const btn = el('button', className, label);
    btn.type = 'button';
    if (title) btn.title = title;
    btn.addEventListener('click', onClick);
    return btn;
}

function svgIcon(pathMarkup, size) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', size || 12);
    svg.setAttribute('height', size || 12);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.innerHTML = pathMarkup; // constant markup defined in this file only
    return svg;
}

// ─── Event listeners ─────────────────────────────────────────────────────────
videoUrlInput.addEventListener('input', handleUrlInput);
videoUrlInput.addEventListener('paste', () => setTimeout(handleUrlInput, 50));

playPauseBtn.addEventListener('click', togglePlayback);
prevFrameBtn.addEventListener('click', () => seekRelative(-1));
nextFrameBtn.addEventListener('click', () => seekRelative(1));

timelineZoomInput.addEventListener('input', (e) => {
    timelineZoom = parseFloat(e.target.value);
    resizeTimelineCanvas();
    drawTimeline();
});

btnFitScreen.addEventListener('click', () => {
    timelineZoom = 1;
    timelineZoomInput.value = 1;
    resizeTimelineCanvas();
    drawTimeline();
});

addRegionBtn.addEventListener('click', addNewRegion);
addBookmarkBtnAlt.addEventListener('click', addNewBookmark);
addBookmarkBtn.addEventListener('click', addNewBookmark);
btnRemoveSelected.addEventListener('click', removeSelectedRegions);
btnClearAll.addEventListener('click', clearAllRegions);
selectAllRegions.addEventListener('change', (e) => toggleSelectAll(e.target.checked));
exportSelectedBtn.addEventListener('click', exportSelectedClips);
aiGenerateCutsBtn.addEventListener('click', generateCutsViaAI);

quickDownloadBtn.addEventListener('click', quickDownloadFull);
if (toggleSettingsBtn) toggleSettingsBtn.addEventListener('click', () => {
    settingsPopover.classList.toggle('hidden');
    toggleSettingsBtn.classList.toggle('open', !settingsPopover.classList.contains('hidden'));
});
if (closeSettingsBtn) closeSettingsBtn.addEventListener('click', () => {
    settingsPopover.classList.add('hidden');
    if (toggleSettingsBtn) toggleSettingsBtn.classList.remove('open');
});

saveDirInput.addEventListener('input', () => { saveDirEdited = true; });

volumeSlider.addEventListener('input', (e) => {
    const vol = parseInt(e.target.value, 10);
    setVolume(vol);
    if (tlVolumeSlider) tlVolumeSlider.value = vol;
});

if (tlVolumeSlider) {
    tlVolumeSlider.addEventListener('input', (e) => {
        const vol = parseInt(e.target.value, 10);
        setVolume(vol);
        volumeSlider.value = vol;
    });
}

function syncCutMode(value) {
    if (clipCutModeSelect) clipCutModeSelect.value = value;
    if (rpCutModeSelect) rpCutModeSelect.value = value;
}
if (clipCutModeSelect) clipCutModeSelect.addEventListener('change', (e) => syncCutMode(e.target.value));
if (rpCutModeSelect) rpCutModeSelect.addEventListener('change', (e) => syncCutMode(e.target.value));

timelineCanvas.addEventListener('mousedown', handleTimelineMouseDown);
window.addEventListener('mousemove', handleTimelineMouseMove);
window.addEventListener('mouseup', handleTimelineMouseUp);

document.querySelectorAll('.tab-header').forEach(header => {
    header.addEventListener('click', () => {
        document.querySelectorAll('.tab-header').forEach(h => h.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        header.classList.add('active');
        const panel = document.getElementById(header.dataset.tab);
        if (panel) panel.classList.add('active');
    });
});

let lastLoadedUrl = "";
function handleUrlInput() {
    const url = videoUrlInput.value.trim();
    if (url === lastLoadedUrl) return;
    if (url.startsWith('http://') || url.startsWith('https://') || extractYoutubeId(url)) {
        lastLoadedUrl = url;
        loadVideoUrl();
    }
}

// ─── Queue + SSE ─────────────────────────────────────────────────────────────
const ACTIVE_STATUSES = ['pending', 'downloading', 'clipping', 'analyzing'];
const sse = new EventSource('/api/stream');

sse.onmessage = function (event) {
    let msg;
    try {
        msg = JSON.parse(event.data);
    } catch (e) {
        return;
    }
    if (msg.type === 'init') {
        knownTasks = new Map((msg.data || []).map(t => [t.id, t]));
        renderQueue();
        setStatusText('Ready');
    } else if (msg.type === 'task_update') {
        handleTaskUpdate(msg.data);
    }
};

sse.onerror = function () {
    setStatusText('Reconnecting to server...', 'busy');
};

function handleTaskUpdate(task) {
    if (!task || !task.id) return;

    if (task.status === 'removed') {
        knownTasks.delete(task.id);
        renderQueue();
        return;
    }

    const previous = knownTasks.get(task.id);
    knownTasks.set(task.id, Object.assign({}, previous || {}, task));
    const merged = knownTasks.get(task.id);

    renderQueue();

    if (task.id === aiTaskId) {
        handleAiTaskUpdate(merged);
    } else if (merged.status === 'failed') {
        setStatusText(`Failed: ${merged.title} -- ${merged.error || 'unknown error'}`, 'error');
    } else if (merged.status === 'completed') {
        setStatusText(`Finished: ${merged.title}`);
    }
}

function renderQueue() {
    const list = Array.from(knownTasks.values())
        .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

    queueList.innerHTML = '';
    if (list.length === 0) {
        queueList.appendChild(el('div', 'empty-queue-msg', 'Queue is empty'));
    } else {
        list.forEach(task => queueList.appendChild(createQueueCard(task)));
    }

    const activeCount = list.filter(t => ACTIVE_STATUSES.includes(t.status)).length;
    document.getElementById('active-download-count').textContent = `${activeCount} active`;
}

function createQueueCard(task) {
    const card = el('div', 'queue-card');
    card.id = `task-card-${task.id}`;

    const top = el('div', 'queue-card-top');
    const thumb = el('div', 'queue-card-thumb');
    thumb.appendChild(svgIcon('<polygon points="5,3 19,12 5,21" fill="currentColor" stroke="none"/>', 14));
    top.appendChild(thumb);

    const meta = el('div', 'queue-card-meta');
    meta.appendChild(el('span', 'queue-title', task.title || 'Untitled'));
    const kindLabel = task.type === 'analysis' ? 'AI Analysis'
        : task.type === 'clip' ? 'Clip Export' : 'Full Video';
    meta.appendChild(el('span', 'queue-subtitle', kindLabel));
    top.appendChild(meta);

    const actions = el('div', 'queue-card-actions action-col');
    const finished = !ACTIVE_STATUSES.includes(task.status);
    if (finished) {
        actions.appendChild(iconButton('✖', 'remove-btn icon-btn',
            () => removeTask(task.id), 'Remove from list'));
    } else {
        actions.appendChild(iconButton('Cancel', 'cancel-btn icon-btn',
            () => cancelTask(task.id), 'Cancel this job'));
    }
    top.appendChild(actions);
    card.appendChild(top);

    const progressRow = el('div', 'queue-card-progress-row');
    const barContainer = el('div', 'progress-container');
    const barBg = el('div', 'progress-bar-bg');
    const fill = el('div', 'progress-bar-fill');
    const pct = Math.max(0, Math.min(100, Number(task.progress) || 0));
    fill.style.width = `${pct}%`;
    barBg.appendChild(fill);
    barContainer.appendChild(barBg);
    progressRow.appendChild(barContainer);
    progressRow.appendChild(el('span', 'progress-text', `${pct.toFixed(0)}%`));
    card.appendChild(progressRow);

    const stats = el('div', 'queue-card-stats');
    stats.appendChild(el('span', `status-badge status-${task.status}`, task.status));
    const speedEta = el('div', 'queue-card-speed-eta');
    speedEta.appendChild(el('span', 'speed-stat', task.speed || 'N/A'));
    speedEta.appendChild(el('span', 'divider', '|'));
    speedEta.appendChild(el('span', 'eta-stat', task.eta || 'N/A'));
    stats.appendChild(speedEta);
    card.appendChild(stats);

    if (task.status === 'failed' && task.error) {
        card.appendChild(el('div', 'queue-card-error', task.error));
    }
    return card;
}

window.cancelTask = function (taskId) {
    postJson('/api/cancel', { task_id: taskId });
};

window.removeTask = function (taskId) {
    postJson('/api/remove', { task_id: taskId }).then(data => {
        if (data && data.success) {
            knownTasks.delete(taskId);
            renderQueue();
        }
    });
};

function postJson(path, payload) {
    return fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    }).then(res => res.json()).catch(() => ({}));
}

// ─── YouTube player ──────────────────────────────────────────────────────────
function initYouTubePlayer(videoId) {
    if (ytPlayer) {
        try { ytPlayer.destroy(); } catch (e) {}
    }

    document.getElementById('yt-player').classList.remove('hidden');
    document.getElementById('local-player').classList.add('hidden');
    document.getElementById('player-placeholder').classList.add('hidden');

    ytPlayer = new YT.Player('yt-player', {
        height: '100%',
        width: '100%',
        videoId: videoId,
        playerVars: { 'playsinline': 1, 'controls': 0, 'rel': 0 },
        events: { 'onReady': onPlayerReady, 'onStateChange': onPlayerStateChange }
    });
    activePlayerType = 'youtube';
}

function onPlayerReady(event) {
    videoDuration = ytPlayer.getDuration() || videoDuration;
    metaDuration.textContent = formatTimeHHMMSS(videoDuration);
    enableEditorControls();
    resizeTimelineCanvas();
    drawTimeline();

    setVolume(parseInt(volumeSlider.value, 10));

    if (playerPollInterval) clearInterval(playerPollInterval);
    playerPollInterval = setInterval(() => {
        if (isPlaying && ytPlayer && ytPlayer.getCurrentTime) {
            currentTime = ytPlayer.getCurrentTime();
            updatePlaybackTimeDisplay();
        }
    }, 100);
}

function onPlayerStateChange(event) {
    isPlaying = event.data === YT.PlayerState.PLAYING;
    setPlayPauseIcon(isPlaying);
}

function setPlayPauseIcon(playing) {
    const markup = playing
        ? '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'
        : '<polygon points="5,3 19,12 5,21"/>';
    if (playPauseBtn) {
        playPauseBtn.innerHTML = '';
        playPauseBtn.appendChild(svgIcon(markup, 14));
    }
    const tlBtn = document.getElementById('btn-play-pause-tl');
    if (tlBtn) {
        tlBtn.innerHTML = '';
        tlBtn.appendChild(svgIcon(markup, 13));
    }
}

function togglePlayback() {
    if (activePlayerType === 'youtube' && ytPlayer) {
        if (isPlaying) ytPlayer.pauseVideo();
        else ytPlayer.playVideo();
    }
}

function seekRelative(seconds) {
    let target = currentTime + seconds;
    if (target < 0) target = 0;
    if (target > videoDuration) target = videoDuration;
    seekToTime(target);
}

function seekToTime(time) {
    currentTime = time;
    if (activePlayerType === 'youtube' && ytPlayer && ytPlayer.seekTo) {
        ytPlayer.seekTo(time, true);
    }
    updatePlaybackTimeDisplay();
}

window.seekToTime = seekToTime;

function updatePlaybackTimeDisplay() {
    timeDisplay.textContent = `${formatTimeHHMMSS(currentTime)} / ${formatTimeHHMMSS(videoDuration)}`;
    drawTimeline();
    updateSelectionRangeDisplay();
}

function setVolume(val) {
    if (activePlayerType === 'youtube' && ytPlayer && ytPlayer.setVolume) {
        ytPlayer.setVolume(val);
    }
}

function enableEditorControls() {
    [addRegionBtn, addBookmarkBtnAlt, btnRemoveSelected, btnClearAll,
     exportSelectedBtn, addBookmarkBtn, aiGenerateCutsBtn, quickDownloadBtn]
        .forEach(btn => { if (btn) btn.disabled = false; });
}

function formatTimeHHMMSS(secs) {
    const total = Math.max(0, Math.floor(Number(secs) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ─── Metadata ────────────────────────────────────────────────────────────────
function loadVideoUrl() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    currentVideoUrl = url;
    activeVideoTitle.textContent = "Loading metadata...";
    setStatusText('Fetching video info...', 'busy');

    postJson('/api/info', { url: url }).then(data => {
        if (data.error) {
            activeVideoTitle.textContent = "Failed to load video";
            setStatusText(`Could not read video: ${data.error}`, 'error');
            return;
        }

        currentVideoTitle = data.title || '';
        activeVideoTitle.textContent = data.title || 'Untitled';

        // Only seed the save directory the first time; never clobber what the
        // user typed afterwards.
        if (!saveDirEdited && data.default_save_dir) {
            saveDirInput.value = data.default_save_dir;
        }

        metaChannel.textContent = data.uploader || 'N/A';
        metaResolution.textContent = data.resolution || 'N/A';
        metaUploadDate.textContent = data.upload_date || 'N/A';
        metaVideoCodec.textContent = data.vcodec || 'N/A';
        metaAudioCodec.textContent = data.acodec || 'N/A';
        metaFps.textContent = data.fps || 'N/A';
        metaSize.textContent = data.filesize || 'N/A';
        if (data.duration_string && data.duration_string !== 'N/A') {
            metaDuration.textContent = data.duration_string;
        }
        if (data.duration && !videoDuration) {
            videoDuration = Number(data.duration);
        }

        // Format options are built as real <option> nodes: format ids and notes
        // come from the remote site and must never be interpolated into HTML.
        customFormatSelect.innerHTML = '';
        const bestOption = document.createElement('option');
        bestOption.value = 'best';
        bestOption.textContent = 'Best Combined';
        customFormatSelect.appendChild(bestOption);

        (data.formats || []).forEach(f => {
            const option = document.createElement('option');
            option.value = String(f.id);
            option.textContent = `${f.resolution} (${f.ext} / ${f.fps}fps) - ${f.note}`;
            customFormatSelect.appendChild(option);
        });

        // Downloads, AI cuts and manual regions all work without the embedded
        // player, so the editor is enabled either way.
        enableEditorControls();

        const videoId = extractYoutubeId(url);
        if (videoId) {
            initYouTubePlayer(videoId);
        } else {
            document.getElementById('player-placeholder').classList.remove('hidden');
            setStatusText('Metadata loaded; the embedded preview is YouTube-only.', 'busy');
        }
        setStatusText('Ready');
    }).catch(() => {
        activeVideoTitle.textContent = "Error connecting to server";
        setStatusText('Failed to reach the server API.', 'error');
    });
}

function extractYoutubeId(url) {
    const patterns = [
        /(?:v=|\/embed\/|\/101\/|\/v\/|youtu\.be\/|\/shorts\/)([a-zA-Z0-9_-]{11})/
    ];
    for (let p of patterns) {
        const m = url.match(p);
        if (m) return m[1];
    }
    return null;
}

// ─── Timeline ────────────────────────────────────────────────────────────────
function resizeTimelineCanvas() {
    const wrapperWidth = timelineScrollWrapper.clientWidth;
    const computedWidth = wrapperWidth * timelineZoom;
    timelineContent.style.width = `${computedWidth}px`;
    timelineCanvas.width = computedWidth;
    timelineCanvas.height = 80;
}

function timeToPixel(time) {
    if (videoDuration === 0) return 0;
    return (time / videoDuration) * timelineCanvas.width;
}

function pixelToTime(x) {
    if (timelineCanvas.width === 0) return 0;
    return (x / timelineCanvas.width) * videoDuration;
}

let drawScheduled = false;
function drawTimeline() {
    if (drawScheduled) return;
    drawScheduled = true;
    requestAnimationFrame(() => {
        drawTimelineRaw();
        drawScheduled = false;
    });
}

function drawTimelineRaw() {
    const ctx = timelineCanvas.getContext('2d');
    const w = timelineCanvas.width;
    const h = timelineCanvas.height;

    ctx.clearRect(0, 0, w, h);

    drawTranscriptDensity(ctx, w, h);
    drawRuler(ctx, w, h);
    drawClipRegions(ctx);
    drawBookmarks(ctx);
    drawPlayhead(ctx, w, h);
}

function drawRuler(ctx, w, h) {
    ctx.fillStyle = '#505a6d';
    ctx.font = '9px Inter, sans-serif';
    ctx.strokeStyle = '#232736';
    ctx.lineWidth = 1;

    const secStep = Math.max(1, Math.round(videoDuration / (w / 120)));
    for (let time = 0; time <= videoDuration; time += secStep) {
        const x = timeToPixel(time);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        if (time % (secStep * 5) === 0) {
            ctx.lineTo(x, 15);
            ctx.fillText(formatTimeHHMMSS(time), x + 4, 11);
        } else {
            ctx.lineTo(x, 6);
        }
        ctx.stroke();
    }
}

// Background bars: real transcript importance once the analysis has run,
// otherwise a neutral placeholder so the timeline still reads as a track.
function drawTranscriptDensity(ctx, w, h) {
    const baseline = h / 2 + 10;

    if (aiChunks && aiChunks.length > 0 && videoDuration > 0) {
        const peak = Math.max(...aiChunks.map(c => Number(c.importance) || 0), 0.01);
        aiChunks.forEach(chunk => {
            const x1 = timeToPixel(chunk.start);
            const x2 = timeToPixel(chunk.end);
            const norm = Math.min(1, (Number(chunk.importance) || 0) / peak);
            const barHeight = 6 + norm * 34;
            ctx.fillStyle = norm >= 0.5 ? 'rgba(79,195,247,0.55)' : 'rgba(79,195,247,0.16)';
            ctx.fillRect(x1, baseline - barHeight / 2, Math.max(1, x2 - x1 - 1), barHeight);
        });
        return;
    }

    ctx.strokeStyle = 'rgba(26, 40, 72, 0.6)';
    ctx.lineWidth = 1;
    for (let x = 0; x < w; x += 3) {
        const amp = (Math.sin(x * 0.03) * 0.35 + Math.sin(x * 0.08) * 0.25 + Math.cos(x * 0.005) * 0.4);
        const height = Math.abs(amp) * 22;
        const gradient = ctx.createLinearGradient(x, baseline - height, x, baseline + height);
        gradient.addColorStop(0, 'rgba(79,195,247,0.15)');
        gradient.addColorStop(0.5, 'rgba(79,195,247,0.08)');
        gradient.addColorStop(1, 'rgba(79,195,247,0.15)');
        ctx.strokeStyle = gradient;
        ctx.beginPath();
        ctx.moveTo(x, baseline - height);
        ctx.lineTo(x, baseline + height);
        ctx.stroke();
    }
}

function drawClipRegions(ctx) {
    clipRegions.forEach((region, idx) => {
        const xStart = timeToPixel(region.start);
        const xEnd = timeToPixel(region.end);
        const isActive = (idx === activeRegionIndex);

        ctx.fillStyle = region.enabled ? 'rgba(79, 195, 247, 0.12)' : 'rgba(61, 79, 110, 0.1)';
        ctx.fillRect(xStart, 20, xEnd - xStart, 45);

        ctx.strokeStyle = region.enabled ? (isActive ? '#80d8ff' : '#4fc3f7') : '#3d4f6e';
        ctx.lineWidth = isActive ? 2.5 : 1.5;
        ctx.strokeRect(xStart, 20, xEnd - xStart, 45);

        ctx.fillStyle = '#80d8ff';
        ctx.fillRect(xStart - 1, 20, 3, 45);
        ctx.fillRect(xEnd - 2, 20, 3, 45);

        if (isActive || region.enabled) {
            ctx.fillStyle = '#4fc3f7';
            ctx.beginPath();
            ctx.arc(xStart, 20, 4, 0, Math.PI * 2);
            ctx.arc(xEnd, 20, 4, 0, Math.PI * 2);
            ctx.fill();
        }

        ctx.fillStyle = '#ffffff';
        ctx.font = '11px Inter, sans-serif';
        ctx.fillText(region.label || `Segment ${idx + 1}`, xStart + 10, 38);
    });
}

function drawBookmarks(ctx) {
    bookmarks.forEach(bm => {
        const x = timeToPixel(bm.time);
        ctx.fillStyle = '#4fc3f7';
        ctx.beginPath();
        ctx.moveTo(x, 12);
        ctx.lineTo(x + 4, 18);
        ctx.lineTo(x, 24);
        ctx.lineTo(x - 4, 18);
        ctx.closePath();
        ctx.fill();
    });
}

function drawPlayhead(ctx, w, h) {
    const playheadX = timeToPixel(currentTime);
    ctx.strokeStyle = '#90caf9';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(playheadX, 0);
    ctx.lineTo(playheadX, h);
    ctx.stroke();

    ctx.fillStyle = '#4fc3f7';
    ctx.beginPath();
    ctx.arc(playheadX, 8, 4, 0, Math.PI * 2);
    ctx.fill();
}

function handleTimelineMouseDown(e) {
    if (videoDuration === 0) return;

    const rect = timelineCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const clickTime = pixelToTime(x);

    for (let i = 0; i < clipRegions.length; i++) {
        const region = clipRegions[i];
        if (!region.enabled) continue;

        const xStart = timeToPixel(region.start);
        const xEnd = timeToPixel(region.end);

        if (Math.abs(x - xStart) <= 10) {
            activeDrag = { type: 'clip-start', index: i };
            activeRegionIndex = i;
            updateSelectionRangeDisplay();
            drawTimeline();
            return;
        }
        if (Math.abs(x - xEnd) <= 10) {
            activeDrag = { type: 'clip-end', index: i };
            activeRegionIndex = i;
            updateSelectionRangeDisplay();
            drawTimeline();
            return;
        }
        if (x > xStart && x < xEnd && y > 20 && y < 65) {
            activeDrag = {
                type: 'clip-move', index: i, startX: x,
                origStart: region.start, origEnd: region.end
            };
            activeRegionIndex = i;
            updateSelectionRangeDisplay();
            drawTimeline();
            return;
        }
    }

    activeDrag = { type: 'playhead' };
    seekToTime(clickTime);
}

function handleTimelineMouseMove(e) {
    if (!activeDrag || videoDuration === 0) return;

    const rect = timelineCanvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(e.clientX - rect.left, timelineCanvas.width));
    const targetTime = pixelToTime(x);

    if (activeDrag.type === 'playhead') {
        seekToTime(targetTime);
    } else if (activeDrag.type === 'clip-start') {
        const region = clipRegions[activeDrag.index];
        if (targetTime < region.end) {
            region.start = parseFloat(targetTime.toFixed(2));
            updateRegionsTable();
        }
    } else if (activeDrag.type === 'clip-end') {
        const region = clipRegions[activeDrag.index];
        if (targetTime > region.start) {
            region.end = parseFloat(targetTime.toFixed(2));
            updateRegionsTable();
        }
    } else if (activeDrag.type === 'clip-move') {
        const region = clipRegions[activeDrag.index];
        const dt = pixelToTime(x - activeDrag.startX);
        const newStart = activeDrag.origStart + dt;
        const newEnd = activeDrag.origEnd + dt;
        if (newStart >= 0 && newEnd <= videoDuration) {
            region.start = parseFloat(newStart.toFixed(2));
            region.end = parseFloat(newEnd.toFixed(2));
            updateRegionsTable();
        }
    }

    drawTimeline();
    updateSelectionRangeDisplay();
}

function handleTimelineMouseUp() {
    activeDrag = null;
}

function updateSelectionRangeDisplay() {
    const region = (activeRegionIndex !== null && activeRegionIndex < clipRegions.length)
        ? clipRegions[activeRegionIndex]
        : (clipRegions.length > 0 ? clipRegions[0] : null);

    rangeStart.textContent = formatTimeHHMMSS(region ? region.start : 0);
    rangeEnd.textContent = formatTimeHHMMSS(region ? region.end : 0);
    rangeDuration.textContent = formatTimeHHMMSS(region ? region.end - region.start : 0);
}

// ─── Regions ─────────────────────────────────────────────────────────────────
function addNewRegion() {
    const duration = 10;
    let start = parseFloat(currentTime.toFixed(2));
    let end = parseFloat((currentTime + duration).toFixed(2));

    if (end > videoDuration) {
        end = videoDuration;
        start = Math.max(0, end - duration);
    }

    clipRegions.push({
        id: Date.now(),
        start: start,
        end: end,
        label: `Segment ${clipRegions.length + 1}`,
        enabled: true
    });

    activeRegionIndex = clipRegions.length - 1;
    updateRegionsTable();
    updateSelectionRangeDisplay();
    drawTimeline();
}

function updateRegionsTable() {
    regionsList.innerHTML = '';

    if (clipRegions.length === 0) {
        const row = el('tr', 'empty-row');
        const cell = el('td', null, 'No clipping regions defined. Click "Generate AI Cuts" or "+ Add Region" to start.');
        cell.colSpan = 7;
        row.appendChild(cell);
        regionsList.appendChild(row);
        return;
    }

    clipRegions.forEach((region, index) => {
        const tr = document.createElement('tr');
        if (!region.enabled) tr.className = 'disabled-row';
        if (index === activeRegionIndex) tr.style.backgroundColor = 'var(--c-hover)';

        const checkCell = el('td');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.className = 'region-checkbox';
        check.checked = !!region.enabled;
        check.addEventListener('change', () => toggleRegion(index, check.checked));
        checkCell.appendChild(check);
        tr.appendChild(checkCell);

        tr.appendChild(el('td', null, index + 1));

        const nameCell = el('td');
        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.value = region.label || '';
        nameInput.style.cssText = 'margin-bottom:0; font-size:12px; width:180px';
        nameInput.addEventListener('change', () => renameRegion(index, nameInput.value));
        nameCell.appendChild(nameInput);
        tr.appendChild(nameCell);

        tr.appendChild(el('td', 'mono-cell', formatTimeHHMMSS(region.start)));
        tr.appendChild(el('td', 'mono-cell', formatTimeHHMMSS(region.end)));
        tr.appendChild(el('td', 'mono-cell', formatTimeHHMMSS(region.end - region.start)));

        const actions = el('td');
        actions.appendChild(iconButton('▶', 'primary-btn icon-btn',
            () => seekToRegion(index), 'Go to segment'));
        actions.appendChild(iconButton('📋', 'primary-btn icon-btn',
            () => duplicateRegion(index), 'Duplicate'));
        actions.appendChild(iconButton('✖', 'cancel-btn icon-btn',
            () => deleteRegion(index), 'Delete'));
        tr.appendChild(actions);

        regionsList.appendChild(tr);
    });
}

window.toggleRegion = function (idx, checked) {
    if (!clipRegions[idx]) return;
    clipRegions[idx].enabled = checked;
    updateRegionsTable();
    drawTimeline();
};

window.renameRegion = function (idx, label) {
    if (!clipRegions[idx]) return;
    clipRegions[idx].label = label;
    drawTimeline();
};

window.seekToRegion = function (idx) {
    if (!clipRegions[idx]) return;
    activeRegionIndex = idx;
    seekToTime(clipRegions[idx].start);
    updateRegionsTable();
};

window.duplicateRegion = function (idx) {
    const orig = clipRegions[idx];
    if (!orig) return;
    clipRegions.splice(idx + 1, 0, {
        id: Date.now() + Math.random(),
        start: orig.start,
        end: orig.end,
        label: `${orig.label} Copy`,
        enabled: true
    });
    activeRegionIndex = idx + 1;
    updateRegionsTable();
    drawTimeline();
};

window.deleteRegion = function (idx) {
    clipRegions.splice(idx, 1);
    if (activeRegionIndex === idx) {
        activeRegionIndex = clipRegions.length > 0 ? 0 : null;
    } else if (activeRegionIndex > idx) {
        activeRegionIndex--;
    }
    updateRegionsTable();
    updateSelectionRangeDisplay();
    drawTimeline();
};

function removeSelectedRegions() {
    clipRegions = clipRegions.filter(r => !r.enabled);
    activeRegionIndex = clipRegions.length > 0 ? 0 : null;
    updateRegionsTable();
    updateSelectionRangeDisplay();
    drawTimeline();
}

function clearAllRegions() {
    if (!confirm("Clear all clipping regions?")) return;
    clipRegions = [];
    activeRegionIndex = null;
    updateRegionsTable();
    updateSelectionRangeDisplay();
    drawTimeline();
}

function toggleSelectAll(checked) {
    clipRegions.forEach(r => r.enabled = checked);
    updateRegionsTable();
    drawTimeline();
}

// ─── Bookmarks ───────────────────────────────────────────────────────────────
function addNewBookmark() {
    const label = prompt("Marker label:", `Keypoint ${bookmarks.length + 1}`);
    if (label === null) return;

    bookmarks.push({
        time: parseFloat(currentTime.toFixed(2)),
        label: label || `Keypoint ${bookmarks.length + 1}`,
        color: '#ff6b00'
    });
    updateBookmarksGrid();
    drawTimeline();
}

function updateBookmarksGrid() {
    bookmarksList.innerHTML = '';

    if (bookmarks.length === 0) {
        bookmarksList.appendChild(el('p', 'empty-text', 'No markers yet. Mark points of interest on the timeline.'));
        return;
    }

    bookmarks.forEach((bm, idx) => {
        const card = el('div', 'bookmark-card');
        const time = el('div', 'bookmark-time', formatTimeHHMMSS(bm.time));
        time.style.cursor = 'pointer';
        time.addEventListener('click', () => seekToTime(bm.time));
        card.appendChild(time);
        card.appendChild(el('div', 'bookmark-label', bm.label));

        const actions = el('div', 'bm-actions-row');
        actions.appendChild(iconButton('Edit', 'secondary-btn icon-btn', () => renameBookmark(idx)));
        actions.appendChild(iconButton('Delete', 'cancel-btn icon-btn', () => deleteBookmark(idx)));
        card.appendChild(actions);

        bookmarksList.appendChild(card);
    });
}

window.renameBookmark = function (idx) {
    const label = prompt("Edit label:", bookmarks[idx].label);
    if (label !== null) {
        bookmarks[idx].label = label;
        updateBookmarksGrid();
    }
};

window.deleteBookmark = function (idx) {
    bookmarks.splice(idx, 1);
    updateBookmarksGrid();
    drawTimeline();
};

// ─── Downloads & exports ─────────────────────────────────────────────────────
function currentCutMode() {
    return (clipCutModeSelect && clipCutModeSelect.value) || appConfig.clipCutMode || 'precise';
}

function quickDownloadFull() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    // The Format Selector popover wins when it has a specific stream selected.
    const selected = customFormatSelect ? customFormatSelect.value : 'best';
    const preset = formatPresetSelect.value;
    const format = preset === 'audio' ? 'audio'
        : (selected && selected !== 'best' ? selected : 'best');

    postJson('/api/enqueue', {
        url: url,
        save_dir: saveDirInput.value.trim(),
        format: format,
        title: currentVideoTitle || activeVideoTitle.textContent
    }).then(data => {
        if (data && data.error) {
            setStatusText(data.error, 'error');
        } else {
            setStatusText('Download queued', 'busy');
        }
    });
}

function exportSelectedClips() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    const enabledClips = clipRegions.filter(r => r.enabled);
    if (enabledClips.length === 0) {
        setStatusText('Enable at least one clipping region first.', 'error');
        return;
    }

    postJson('/api/clip', {
        url: url,
        save_dir: saveDirInput.value.trim(),
        regions: enabledClips.map(r => ({
            start: r.start, end: r.end, label: r.label, enabled: true
        })),
        title: currentVideoTitle || activeVideoTitle.textContent,
        cut_mode: currentCutMode()
    }).then(data => {
        if (data && data.error) {
            setStatusText(data.error, 'error');
        } else {
            setStatusText(`Queued ${enabledClips.length} clip(s) for export`, 'busy');
        }
    });
}

// ─── AI pipeline: transcript -> analyse -> clip ranges ───────────────────────
const AI_ICON = '<polygon points="12,2 15.09,8.26 22,9.27 17,14.14 18.18,21.02 12,17.77 5.82,21.02 7,14.14 2,9.27 8.91,8.26"/>';

function setAiButtonBusy(busy, label) {
    aiGenerateCutsBtn.disabled = busy;
    aiGenerateCutsBtn.innerHTML = '';
    aiGenerateCutsBtn.appendChild(svgIcon(busy
        ? '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>'
        : AI_ICON, 13));
    aiGenerateCutsBtn.appendChild(document.createTextNode(' ' + (label || 'Generate AI Cuts')));
}

function setAiStatus(text) {
    if (aiStatusEl) aiStatusEl.textContent = text || '';
}

function setPipelineStep(step) {
    for (let i = 1; i <= 3; i++) {
        const node = document.getElementById(`pipe-step-${i}`);
        if (!node) continue;
        node.classList.toggle('done', i < step);
        node.classList.toggle('active', i === step);
        node.classList.toggle('pending', i > step);
    }
}

function generateCutsViaAI() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    if (aiTaskId) {
        setStatusText('An analysis is already running for this video.', 'busy');
        return;
    }

    setAiButtonBusy(true, 'Analyzing...');
    setAiStatus('queued');
    setPipelineStep(1);
    setStatusText('Extracting transcript...', 'busy');

    postJson('/api/ai/analyze', {
        url: url,
        title: currentVideoTitle || activeVideoTitle.textContent
    }).then(data => {
        if (data && data.error) {
            resetAiUi();
            setStatusText(`AI analysis failed: ${data.error}`, 'error');
            return;
        }
        aiTaskId = data.task_id;
    }).catch(() => {
        resetAiUi();
        setStatusText('AI service is unavailable.', 'error');
    });
}

function handleAiTaskUpdate(task) {
    const progress = Number(task.progress) || 0;
    const message = task.eta || '';

    setAiStatus(`${progress.toFixed(0)}% ${message}`);

    if (progress < 18) {
        setPipelineStep(1);
        setStatusText(`Extracting transcript... ${message}`, 'busy');
    } else if (progress < 82) {
        setPipelineStep(2);
        setStatusText(`Analysing transcript... ${message}`, 'busy');
    } else if (task.status !== 'completed') {
        setPipelineStep(3);
        setStatusText(`Building clip ranges... ${message}`, 'busy');
    }

    if (task.status === 'completed') {
        fetchAiResult(task.id);
    } else if (task.status === 'failed' || task.status === 'cancelled') {
        resetAiUi();
        setStatusText(`AI analysis ${task.status}: ${task.error || 'stopped'}`, 'error');
    }
}

function fetchAiResult(taskId) {
    fetch(`/api/ai/result/${encodeURIComponent(taskId)}`)
        .then(res => res.json())
        .then(result => {
            resetAiUi();

            if (result.error) {
                setStatusText(`AI analysis failed: ${result.error}`, 'error');
                return;
            }

            aiChunks = Array.isArray(result.chunks) ? result.chunks : null;
            const sections = Array.isArray(result.sections) ? result.sections : [];

            if (aiChunks && tlLegendText) {
                tlLegendText.textContent =
                    `Transcript density across ${aiChunks.length} chunks - taller bars scored higher`;
            }

            const added = applyAiSections(sections);
            drawTimeline();

            if (added === 0) {
                setStatusText('Analysis finished, but no clip ranges were produced.', 'busy');
            } else {
                setStatusText(
                    `Added ${added} clip range(s) from the transcript ` +
                    `(${result.transcript_entries || 0} caption cues, ${result.language || 'unknown'}).`
                );
            }
        })
        .catch(() => {
            resetAiUi();
            setStatusText('Could not read the analysis result.', 'error');
        });
}

function applyAiSections(sections) {
    let added = 0;

    sections.forEach(sec => {
        let start = Math.max(0, Number(sec.start) || 0);
        let end = Number(sec.end) || (start + 15);
        if (end <= start) return;

        if (videoDuration > 0) {
            start = Math.min(start, videoDuration);
            end = Math.min(end, videoDuration);
        }
        if (end - start < 1) return;

        const duplicate = clipRegions.some(
            r => Math.abs(r.start - start) < 2 && Math.abs(r.end - end) < 2
        );
        if (duplicate) return;

        clipRegions.push({
            id: Date.now() + Math.random(),
            start: parseFloat(start.toFixed(2)),
            end: parseFloat(end.toFixed(2)),
            label: (sec.label || 'AI clip').slice(0, 40),
            enabled: true
        });
        added++;
    });

    clipRegions.sort((a, b) => a.start - b.start);
    activeRegionIndex = clipRegions.length > 0 ? clipRegions.length - 1 : null;
    updateRegionsTable();
    updateSelectionRangeDisplay();
    return added;
}

function resetAiUi() {
    aiTaskId = null;
    setAiButtonBusy(false);
    setAiStatus('');
    setPipelineStep(0);
}

// ─── Status bar ──────────────────────────────────────────────────────────────
function setStatusText(msg, type) {
    const statusEl = document.getElementById('status-text');
    const dot = document.querySelector('.status-dot');
    if (!statusEl) return;
    statusEl.textContent = msg;
    if (dot) {
        dot.style.background = type === 'error' ? '#ef5350' : type === 'busy' ? '#ffa726' : '#4fc3f7';
        dot.style.boxShadow = type === 'error'
            ? '0 0 6px #ef5350'
            : type === 'busy' ? '0 0 6px #ffa726' : '0 0 8px rgba(79,195,247,.8)';
    }
}

setStatusText('Ready');
setPipelineStep(0);
