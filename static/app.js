// YT-DLP Studio Frontend Application Logic

// State Variables
let videoDuration = 0;
let currentTime = 0;
let isPlaying = false;
let ytPlayer = null;
let activePlayerType = 'youtube'; // 'youtube' or 'local'
let playerPollInterval = null;

// Timeline & Drawing State
let timelineZoom = 1;
let bookmarks = []; // {time, label, color}
let clipRegions = []; // {id, start, end, label, enabled}
let activeDrag = null; // {type: 'playhead'|'clip-start'|'clip-end'|'clip-move', index: number, startX: number, origStart: number, origEnd: number}
let activeRegionIndex = null; // Index of the currently highlighted region

// Elements
const videoUrlInput = document.getElementById('video-url');
const loadVideoBtn = document.getElementById('load-video-btn');
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

const queueList = document.getElementById('queue-list');
const saveDirInput = document.getElementById('save-dir');
const quickDownloadBtn = document.getElementById('quick-download-btn');
const formatPresetSelect = document.getElementById('download-format');

const toggleSettingsBtn = document.getElementById('toggle-settings-btn');
const settingsPopover = document.getElementById('settings-popover');
const closeSettingsBtn = document.getElementById('close-settings-btn');
const customFormatSelect = document.getElementById('custom-format');

// Metadata elements
const metaChannel = document.getElementById('meta-channel');
const metaResolution = document.getElementById('meta-resolution');
const metaDuration = document.getElementById('meta-duration');
const metaUploadDate = document.getElementById('meta-upload-date');
const metaVideoCodec = document.getElementById('meta-video-codec');
const metaAudioCodec = document.getElementById('meta-audio-codec');
const metaFps = document.getElementById('meta-fps');
const metaSize = document.getElementById('meta-size');

// Selection display elements
const rangeStart = document.getElementById('range-start');
const rangeEnd = document.getElementById('range-end');
const rangeDuration = document.getElementById('range-duration');

// Volume element
const volumeSlider = document.getElementById('playback-volume-slider');
const appConfig = (window.APP_CONFIG && typeof window.APP_CONFIG === 'object') ? window.APP_CONFIG : {};

// Serverless (Vercel) mode: no persistent backend process, downloads are
// streamed through the API straight into the browser. The page may be served
// from a CDN, so mode is detected at runtime via /api/health (falling back to
// the boot config injected by the self-hosted server when present).
let isServerless = false;
let aiEnabledOnServer = (typeof appConfig.aiEnabled === 'boolean') ? appConfig.aiEnabled : null;

function applyDeploymentMode() {
    if (isServerless) {
        saveDirInput.value = "Browser downloads";
        saveDirInput.disabled = true;
        saveDirInput.title = "On this deployment files are saved by your browser (Downloads folder or the location you pick).";
        const browseBtn = document.querySelector('.dir-browse-btn');
        if (browseBtn) browseBtn.disabled = true;
    } else {
        saveDirInput.value = appConfig.defaultSaveDir || "C:\\Users\\LCS\\Downloads";
        startServerStream();
    }
}

function detectDeployment() {
    fetch('/api/health')
        .then(res => (res.ok ? res.json() : null))
        .then(data => {
            if (data && data.ok) {
                isServerless = !!data.serverless;
                if (typeof data.ai_enabled === 'boolean') aiEnabledOnServer = data.ai_enabled;
            }
        })
        .catch(() => {})
        .then(applyDeploymentMode);
}
detectDeployment();

// Event Listeners
loadVideoBtn.addEventListener('click', loadVideoUrl);
videoUrlInput.addEventListener('input', handleUrlInput);
videoUrlInput.addEventListener('paste', () => {
    setTimeout(handleUrlInput, 50);
});

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
toggleSettingsBtn.addEventListener('click', () => settingsPopover.classList.toggle('hidden'));
closeSettingsBtn.addEventListener('click', () => settingsPopover.classList.add('hidden'));

volumeSlider.addEventListener('input', (e) => {
    const vol = parseInt(e.target.value);
    setVolume(vol);
    // Sync timeline volume slider
    const tlVol = document.getElementById('tl-volume');
    if (tlVol) tlVol.value = vol;
});

const tlVolumeSlider = document.getElementById('tl-volume');
if (tlVolumeSlider) {
    tlVolumeSlider.addEventListener('input', (e) => {
        const vol = parseInt(e.target.value);
        setVolume(vol);
        if (volumeSlider) volumeSlider.value = vol;
    });
}

// Timeline Canvas Event Listeners for dragging and scrubbing
timelineCanvas.addEventListener('mousedown', handleTimelineMouseDown);
window.addEventListener('mousemove', handleTimelineMouseMove);
window.addEventListener('mouseup', handleTimelineMouseUp);

// Tabs UI Logic
document.querySelectorAll('.tab-header').forEach(header => {
    header.addEventListener('click', () => {
        document.querySelectorAll('.tab-header').forEach(h => h.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        header.classList.add('active');
        document.getElementById(header.dataset.tab).classList.add('active');
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

// SSE Streaming for Queue (self-hosted backend only; Vercel runs a client-side queue)
let lastVideoMeta = null;
function startServerStream() {
    const sse = new EventSource('/api/stream');
    sse.onmessage = function(event) {
        const msg = JSON.parse(event.data);
        if (msg.type === 'init') {
            renderQueue(msg.data);
        } else if (msg.type === 'task_update') {
            updateQueueItemUI(msg.data);
        }
    };
}

function renderQueue(tasks) {
    if (tasks.length === 0) {
        queueList.innerHTML = `<div class="empty-queue-msg">Queue is empty</div>`;
        document.getElementById('active-download-count').innerText = "0 active";
        document.getElementById('nav-queue-count').innerText = "0";
        return;
    }
    queueList.innerHTML = '';
    let activeCount = 0;
    tasks.forEach(task => {
        if (['pending', 'downloading', 'clipping'].includes(task.status)) {
            activeCount++;
        }
        queueList.appendChild(createQueueCard(task));
    });
    document.getElementById('active-download-count').innerText = `${activeCount} active`;
    document.getElementById('nav-queue-count').innerText = String(activeCount);
}

function createQueueCard(task) {
    const div = document.createElement('div');
    div.className = 'queue-card';
    div.id = `task-card-${task.id}`;
    
    const pct = (task.progress || 0).toFixed(0);
    const mockThumb = task.thumbnail || "https://img.youtube.com/vi/dQw4w9WgXcQ/default.jpg";
    
    const isFinished = !['pending', 'downloading', 'clipping'].includes(task.status);
    
    div.innerHTML = `
        <div class="queue-card-top">
            <img class="queue-card-thumb" src="${mockThumb}" alt="thumbnail">
            <div class="queue-card-meta">
                <span class="queue-title" title="${task.title}">${task.title}</span>
                <span class="queue-subtitle">${task.type === 'clip' ? 'Clip Export' : 'Full Video'} (${task.format || 'MP4'})</span>
            </div>
            <div class="queue-card-actions action-col">
                ${isFinished ? 
                    `<button class="remove-btn icon-btn" onclick="removeTask('${task.id}')" title="Remove" style="padding:2px 6px;font-size:9px;background-color:transparent;color:var(--text-muted);border:1px solid var(--border-dark);">✖</button>` : 
                    `<button class="cancel-btn icon-btn" onclick="cancelTask('${task.id}')" style="padding:2px 6px;font-size:9px">Cancel</button>`}
            </div>
        </div>
        <div class="queue-card-progress-row">
            <div class="progress-container">
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: ${pct}%"></div>
                </div>
            </div>
            <span class="progress-text">${pct}%</span>
        </div>
        <div class="queue-card-stats">
            <span class="status-badge status-${task.status}">${task.status}</span>
            <div class="queue-card-speed-eta">
                <span class="speed-stat">${task.speed || 'N/A'}</span>
                <span class="divider">|</span>
                <span class="eta-stat">ETA ${task.eta || 'N/A'}</span>
            </div>
        </div>
    `;
    return div;
}

function refreshActiveQueueCount() {
    const activeStatuses = ['pending', 'downloading', 'clipping'];
    if (isServerless) {
        const tasks = Object.values(clientTasks);
        const activeCount = tasks.filter(t => activeStatuses.includes(t.status)).length;
        document.getElementById('active-download-count').innerText = `${activeCount} active`;
        document.getElementById('nav-queue-count').innerText = String(activeCount);
        return;
    }
    fetch('/api/queue')
        .then(res => res.json())
        .then(tasks => {
            const activeCount = (tasks || []).filter(t => activeStatuses.includes(t.status)).length;
            document.getElementById('active-download-count').innerText = `${activeCount} active`;
            document.getElementById('nav-queue-count').innerText = String(activeCount);
        })
        .catch(() => {});
}

function rerenderWholeQueue() {
    if (isServerless) {
        renderQueue(Object.values(clientTasks));
        return;
    }
    fetch('/api/queue')
        .then(res => res.json())
        .then(renderQueue)
        .catch(() => {});
}

function updateQueueItemUI(task) {
    if (task.status === 'removed') {
        const card = document.getElementById(`task-card-${task.id}`);
        if (card) card.remove();
        if (isServerless) delete clientTasks[task.id];
        rerenderWholeQueue();
        refreshActiveQueueCount();
        return;
    }
    const card = document.getElementById(`task-card-${task.id}`);
    if (card) {
        const pct = (task.progress || 0).toFixed(0);
        card.querySelector('.progress-bar-fill').style.width = `${pct}%`;
        card.querySelector('.progress-text').innerText = `${pct}%`;
        card.querySelector('.speed-stat').innerText = task.speed || 'N/A';
        card.querySelector('.eta-stat').innerText = `ETA ${task.eta || 'N/A'}`;

        const badge = card.querySelector('.status-badge');
        badge.className = `status-badge status-${task.status}`;
        badge.innerText = task.status;

        const actionCol = card.querySelector('.action-col');
        if (!['pending', 'downloading', 'clipping'].includes(task.status)) {
            actionCol.innerHTML = `<button class="remove-btn icon-btn" onclick="removeTask('${task.id}')" title="Remove" style="padding:2px 6px;font-size:9px;background-color:transparent;color:var(--text-muted);border:1px solid var(--border-dark);">✖</button>`;
        }

        refreshActiveQueueCount();
    } else {
        // Refresh entire list if it's a new task
        rerenderWholeQueue();
        refreshActiveQueueCount();
    }
}

window.cancelTask = function(taskId) {
    if (isServerless) {
        const task = clientTasks[taskId];
        if (task && task.abort) task.abort.abort();
        return;
    }
    fetch('/api/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId })
    });
};

window.removeTask = function(taskId) {
    if (isServerless) {
        delete clientTasks[taskId];
        const card = document.getElementById(`task-card-${taskId}`);
        if (card) card.remove();
        rerenderWholeQueue();
        refreshActiveQueueCount();
        return;
    }
    fetch('/api/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId })
    }).then(res => res.json())
      .then(data => {
          if (data.success) {
              const card = document.getElementById(`task-card-${taskId}`);
              if (card) card.remove();
              fetch('/api/queue')
                  .then(res => res.json())
                  .then(renderQueue);
          }
      });
};

// YouTube player initialization
function initYouTubePlayer(videoId) {
    if (ytPlayer) {
        try {
            ytPlayer.destroy();
        } catch (e) {}
    }
    
    document.getElementById('yt-player').classList.remove('hidden');
    document.getElementById('local-player').classList.add('hidden');
    document.getElementById('player-placeholder').classList.add('hidden');
    
    ytPlayer = new YT.Player('yt-player', {
        height: '100%',
        width: '100%',
        videoId: videoId,
        playerVars: {
            'playsinline': 1,
            'controls': 0,
            'rel': 0
        },
        events: {
            'onReady': onPlayerReady,
            'onStateChange': onPlayerStateChange
        }
    });
    activePlayerType = 'youtube';
}

function onPlayerReady(event) {
    videoDuration = ytPlayer.getDuration();
    metaDuration.innerText = formatTimeHHMMSS(videoDuration);
    enableEditorControls();
    resizeTimelineCanvas();
    drawTimeline();
    
    // Set initial volume matching slider
    setVolume(parseInt(volumeSlider.value));
    
    if (playerPollInterval) clearInterval(playerPollInterval);
    playerPollInterval = setInterval(() => {
        if (isPlaying && ytPlayer && ytPlayer.getCurrentTime) {
            currentTime = ytPlayer.getCurrentTime();
            updatePlaybackTimeDisplay();
        }
    }, 100);
}

function onPlayerStateChange(event) {
    if (event.data === YT.PlayerState.PLAYING) {
        isPlaying = true;
        setPlayPauseIcon(true);
    } else {
        isPlaying = false;
        setPlayPauseIcon(false);
    }
}

function setPlayPauseIcon(playing) {
    const pauseSVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`;
    const playSVG  = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>`;
    if (playPauseBtn) playPauseBtn.innerHTML = playing ? pauseSVG : playSVG;
    const tlBtn = document.getElementById('btn-play-pause-tl');
    if (tlBtn) tlBtn.innerHTML = playing ? pauseSVG : playSVG;
}

function togglePlayback() {
    if (activePlayerType === 'youtube' && ytPlayer) {
        if (isPlaying) {
            ytPlayer.pauseVideo();
        } else {
            ytPlayer.playVideo();
        }
    }
}

function seekRelative(seconds) {
    let target = currentTime + seconds;
    if (target < 0) target = 0;
    if (target > videoDuration) target = videoDuration;
    
    currentTime = target;
    if (activePlayerType === 'youtube' && ytPlayer) {
        ytPlayer.seekTo(target, true);
    }
    updatePlaybackTimeDisplay();
}

function updatePlaybackTimeDisplay() {
    timeDisplay.innerText = `${formatTimeHHMMSS(currentTime)} / ${formatTimeHHMMSS(videoDuration)}`;
    drawTimeline();
    updateSelectionRangeDisplay();
}

function setVolume(val) {
    if (activePlayerType === 'youtube' && ytPlayer && ytPlayer.setVolume) {
        ytPlayer.setVolume(val);
    }
}

function enableEditorControls() {
    addRegionBtn.disabled = false;
    addBookmarkBtnAlt.disabled = false;
    btnRemoveSelected.disabled = false;
    btnClearAll.disabled = false;
    exportSelectedBtn.disabled = false;
    addBookmarkBtn.disabled = false;
    aiGenerateCutsBtn.disabled = false;
    quickDownloadBtn.disabled = false;
}

// Convert seconds to HH:MM:SS format
function formatTimeHHMMSS(secs) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    
    const hh = h.toString().padStart(2, '0');
    const mm = m.toString().padStart(2, '0');
    const ss = s.toString().padStart(2, '0');
    
    return `${hh}:${mm}:${ss}`;
}

// Load Video Url metadata
function loadVideoUrl() {
    const url = videoUrlInput.value.trim();
    if (!url) return;
    
    activeVideoTitle.innerText = "Loading video uploader & metadata presets...";
    
    fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            activeVideoTitle.innerText = "Failed to load video";
            alert(`Error: ${data.error}`);
            return;
        }
        
        activeVideoTitle.innerText = data.title;
        lastVideoMeta = data;
        if (!isServerless && data.default_save_dir) {
            saveDirInput.value = data.default_save_dir;
        }
        
        // Populate metadata panel
        metaChannel.innerText = data.uploader || 'N/A';
        metaResolution.innerText = data.resolution || 'N/A';
        metaUploadDate.innerText = data.upload_date || 'N/A';
        metaVideoCodec.innerText = data.vcodec || 'N/A';
        metaAudioCodec.innerText = data.acodec || 'N/A';
        metaFps.innerText = data.fps || 'N/A';
        metaSize.innerText = data.filesize || 'N/A';
        
        // Setup advanced quality selectors
        customFormatSelect.innerHTML = '<option value="best">Best Quality (Combined)</option>';
        data.formats.forEach(f => {
            customFormatSelect.innerHTML += `<option value="${f.id}">${f.resolution} (${f.ext} / ${f.fps}fps) - ${f.note}</option>`;
        });
        
        const videoId = extractYoutubeId(url);
        if (videoId) {
            initYouTubePlayer(videoId);
        } else {
            alert("Loaded metadata successfully, but player embed is not supported for this URL.");
        }
    })
    .catch(err => {
        activeVideoTitle.innerText = "Error connecting to server";
        alert("Failed to reach server backend api.");
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

// Timeline Canvas Drawing (Waveform, Ticks, playhead, active regions)
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
    
    // 1. Draw Waveform background (DaVinci style visualizer)
    drawAudioWaveform(ctx, w, h);
    
    // 2. Draw Ticks & ruler
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
    
    // 3. Draw Clipping Regions
    clipRegions.forEach((region, idx) => {
        const xStart = timeToPixel(region.start);
        const xEnd = timeToPixel(region.end);
        
        // Highlight active or standard selection
        const isActive = (idx === activeRegionIndex);
        
        ctx.fillStyle = region.enabled ? 'rgba(79, 195, 247, 0.12)' : 'rgba(61, 79, 110, 0.1)';
        ctx.fillRect(xStart, 20, xEnd - xStart, 45);
        
        ctx.strokeStyle = region.enabled ? (isActive ? '#80d8ff' : '#4fc3f7') : '#3d4f6e';
        ctx.lineWidth = isActive ? 2.5 : 1.5;
        ctx.strokeRect(xStart, 20, xEnd - xStart, 45);
        
        // Draw boundaries drag lines
        ctx.fillStyle = '#80d8ff';
        ctx.fillRect(xStart - 1, 20, 3, 45);
        ctx.fillRect(xEnd - 2, 20, 3, 45);
        
        // Draw handles above region boundaries
        if (isActive || region.enabled) {
            ctx.fillStyle = '#4fc3f7';
            ctx.beginPath();
            ctx.arc(xStart, 20, 4, 0, Math.PI * 2);
            ctx.arc(xEnd, 20, 4, 0, Math.PI * 2);
            ctx.fill();
        }
        
        // Text name tag
        ctx.fillStyle = '#ffffff';
        ctx.font = '11px Inter, sans-serif';
        ctx.fillText(region.label || `Segment ${idx+1}`, xStart + 10, 38);
    });
    
    // 4. Draw Bookmarks
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
    
    // 5. Draw Playhead scrubber line
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

function drawAudioWaveform(ctx, w, h) {
    ctx.strokeStyle = 'rgba(26, 40, 72, 0.6)';
    ctx.lineWidth = 1;
    const midY = h / 2 + 10;
    
    // Render a consistent waveform drawing
    for (let x = 0; x < w; x += 3) {
        const amp = (Math.sin(x * 0.03) * 0.35 + Math.sin(x * 0.08) * 0.25 + Math.cos(x * 0.005) * 0.4);
        const height = Math.abs(amp) * 22;
        // Subtle blue gradient bars
        const gradient = ctx.createLinearGradient(x, midY - height, x, midY + height);
        gradient.addColorStop(0, 'rgba(79,195,247,0.15)');
        gradient.addColorStop(0.5, 'rgba(79,195,247,0.08)');
        gradient.addColorStop(1, 'rgba(79,195,247,0.15)');
        ctx.strokeStyle = gradient;
        ctx.beginPath();
        ctx.moveTo(x, midY - height);
        ctx.lineTo(x, midY + height);
        ctx.stroke();
    }
}

// Mouse events on timeline scrubber ruler
function handleTimelineMouseDown(e) {
    if (videoDuration === 0) return;
    
    const rect = timelineCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    
    const clickTime = pixelToTime(x);
    
    // Check if clicking near a handle of active region
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
            activeDrag = { type: 'clip-move', index: i, startX: x, origStart: region.origStart || region.start, origEnd: region.origEnd || region.end };
            activeDrag.origStart = region.start;
            activeDrag.origEnd = region.end;
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
        const dx = x - activeDrag.startX;
        const dt = pixelToTime(dx);
        
        let newStart = activeDrag.origStart + dt;
        let newEnd = activeDrag.origEnd + dt;
        
        if (newStart >= 0 && newEnd <= videoDuration) {
            region.start = parseFloat(newStart.toFixed(2));
            region.end = parseFloat(newEnd.toFixed(2));
            updateRegionsTable();
        }
    }
    
    drawTimeline();
    updateSelectionRangeDisplay();
}

function handleTimelineMouseUp(e) {
    activeDrag = null;
}

function seekToTime(time) {
    currentTime = time;
    if (activePlayerType === 'youtube' && ytPlayer && ytPlayer.seekTo) {
        ytPlayer.seekTo(time, true);
    }
    updatePlaybackTimeDisplay();
}

function updateSelectionRangeDisplay() {
    if (activeRegionIndex !== null && activeRegionIndex < clipRegions.length) {
        const region = clipRegions[activeRegionIndex];
        rangeStart.innerText = formatTimeHHMMSS(region.start);
        rangeEnd.innerText = formatTimeHHMMSS(region.end);
        rangeDuration.innerText = formatTimeHHMMSS(region.end - region.start);
    } else if (clipRegions.length > 0) {
        const region = clipRegions[0];
        rangeStart.innerText = formatTimeHHMMSS(region.start);
        rangeEnd.innerText = formatTimeHHMMSS(region.end);
        rangeDuration.innerText = formatTimeHHMMSS(region.end - region.start);
    } else {
        rangeStart.innerText = "00:00:00";
        rangeEnd.innerText = "00:00:00";
        rangeDuration.innerText = "00:00:00";
    }
}

// Clipping Regions Table manager
function addNewRegion() {
    const duration = 10;
    let start = parseFloat(currentTime.toFixed(2));
    let end = parseFloat((currentTime + duration).toFixed(2));
    
    if (end > videoDuration) {
        end = videoDuration;
        start = Math.max(0, end - duration);
    }
    
    const newRegion = {
        id: Date.now(),
        start: start,
        end: end,
        label: `Segment ${clipRegions.length + 1}`,
        enabled: true
    };
    
    clipRegions.push(newRegion);
    activeRegionIndex = clipRegions.length - 1;
    updateRegionsTable();
    updateSelectionRangeDisplay();
    drawTimeline();
}

function updateRegionsTable() {
    if (clipRegions.length === 0) {
        regionsList.innerHTML = `
            <tr class="empty-row">
                <td colspan="7">No clipping regions defined. Click "+ Add Region" or use AI cuts to start.</td>
            </tr>
        `;
        return;
    }
    
    regionsList.innerHTML = '';
    clipRegions.forEach((region, index) => {
        const tr = document.createElement('tr');
        if (!region.enabled) tr.className = 'disabled-row';
        if (index === activeRegionIndex) tr.style.backgroundColor = 'var(--c-hover)';
        
        const durationStr = formatTimeHHMMSS(region.end - region.start);
        
        tr.innerHTML = `
            <td>
                <input type="checkbox" class="region-checkbox" ${region.enabled ? 'checked' : ''} onchange="toggleRegion(${index}, this.checked)">
            </td>
            <td>${index + 1}</td>
            <td>
                <input type="text" value="${region.label}" onchange="renameRegion(${index}, this.value)" style="margin-bottom:0; font-size:12px; width:180px">
            </td>
            <td style="font-family:var(--font-mono);">${formatTimeHHMMSS(region.start)}</td>
            <td style="font-family:var(--font-mono);">${formatTimeHHMMSS(region.end)}</td>
            <td style="font-family:var(--font-mono);">${durationStr}</td>
            <td>
                <button class="primary-btn icon-btn" onclick="seekToRegion(${index})" style="padding:4px 8px; font-size:10px" title="Go to Segment">▶</button>
                <button class="primary-btn icon-btn" onclick="duplicateRegion(${index})" style="padding:4px 8px; font-size:10px" title="Duplicate">📋</button>
                <button class="cancel-btn icon-btn" onclick="deleteRegion(${index})" style="padding:4px 8px; font-size:10px" title="Delete">✖</button>
            </td>
        `;
        regionsList.appendChild(tr);
    });
}

window.toggleRegion = function(idx, checked) {
    clipRegions[idx].enabled = checked;
    updateRegionsTable();
    drawTimeline();
};

window.renameRegion = function(idx, label) {
    clipRegions[idx].label = label;
    drawTimeline();
};

window.seekToRegion = function(idx) {
    activeRegionIndex = idx;
    seekToTime(clipRegions[idx].start);
    updateRegionsTable();
};

window.duplicateRegion = function(idx) {
    const orig = clipRegions[idx];
    const dupl = {
        id: Date.now() + Math.random(),
        start: orig.start,
        end: orig.end,
        label: `${orig.label} Copy`,
        enabled: true
    };
    clipRegions.splice(idx + 1, 0, dupl);
    activeRegionIndex = idx + 1;
    updateRegionsTable();
    drawTimeline();
};

window.deleteRegion = function(idx) {
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
    if (confirm("Are you sure you want to clear all clipping regions?")) {
        clipRegions = [];
        activeRegionIndex = null;
        updateRegionsTable();
        updateSelectionRangeDisplay();
        drawTimeline();
    }
}

function toggleSelectAll(checked) {
    clipRegions.forEach(r => r.enabled = checked);
    updateRegionsTable();
    drawTimeline();
}

// Bookmarks List Management
function addNewBookmark() {
    const label = prompt("Enter Key Point description/label:", `Keypoint ${bookmarks.length + 1}`);
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
    if (bookmarks.length === 0) {
        bookmarksList.innerHTML = `<p class="empty-text">No bookmarks added yet. Tag points of interest on the timeline.</p>`;
        return;
    }
    
    bookmarksList.innerHTML = '';
    bookmarks.forEach((bm, idx) => {
        const card = document.createElement('div');
        card.className = 'bookmark-card';
        card.innerHTML = `
            <div class="bookmark-time" onclick="seekToTime(${bm.time})">${formatTimeHHMMSS(bm.time)}</div>
            <div class="bookmark-label" title="${bm.label}">${bm.label}</div>
            <div style="display:flex;gap:5px;margin-top:auto">
                <button class="secondary-btn icon-btn" onclick="renameBookmark(${idx})" style="padding:4px 6px;font-size:10px">Edit</button>
                <button class="cancel-btn icon-btn" onclick="deleteBookmark(${idx})" style="padding:4px 6px;font-size:10px">Delete</button>
            </div>
        `;
        bookmarksList.appendChild(card);
    });
}

window.renameBookmark = function(idx) {
    const label = prompt("Edit label:", bookmarks[idx].label);
    if (label !== null) {
        bookmarks[idx].label = label;
        updateBookmarksGrid();
    }
};

window.deleteBookmark = function(idx) {
    bookmarks.splice(idx, 1);
    updateBookmarksGrid();
    drawTimeline();
};

// Quick Download Full Video
function quickDownloadFull() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    const format = formatPresetSelect.value;
    const title = activeVideoTitle.innerText;

    if (isServerless) {
        const task = addClientTask({
            title: title,
            type: 'download',
            format: format,
            thumbnail: lastVideoMeta && lastVideoMeta.thumbnail
        });
        enqueueClientJob(() => runClientDownload(task, { url, format }));
        return;
    }

    const saveDir = saveDirInput.value.trim();
    
    fetch('/api/enqueue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            url: url,
            save_dir: saveDir,
            format: format,
            title: title
        })
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            alert(`Error: ${data.error}`);
        }
    });
}

// Export Selected Clipping Regions
function exportSelectedClips() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    const enabledClips = clipRegions.filter(r => r.enabled);
    if (enabledClips.length === 0) {
        alert("Please define and enable at least one clipping region first.");
        return;
    }

    const title = activeVideoTitle.innerText;

    if (isServerless) {
        enabledClips.forEach((region, i) => {
            const task = addClientTask({
                title: `${region.label || `Segment ${i + 1}`}`,
                type: 'clip',
                format: 'MP4',
                thumbnail: lastVideoMeta && lastVideoMeta.thumbnail
            });
            enqueueClientJob(() => runClientClip(task, {
                url,
                start: region.start,
                end: region.end,
                label: region.label || `segment_${i + 1}`,
                title: title
            }));
        });
        return;
    }

    const saveDir = saveDirInput.value.trim();
    
    fetch('/api/clip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            url: url,
            save_dir: saveDir,
            regions: enabledClips,
            title: `Export Clips: ${title}`
        })
    })
    .then(res => res.json())
    .then(data => {
        if (data.error) {
            alert(`Error: ${data.error}`);
        }
    });
}

// ─── Right Panel Helpers ──────────────────────────────────────────────────

// Format Pill Selection
document.querySelectorAll('.pill-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    });
});

// Apply Export Preset
window.applyPreset = function(name) {
    const presetMap = {
        short:   { label: 'Short (9:16) 1080×1920', format: 'best' },
        reels:   { label: 'Reels (9:16) 1080×1920', format: 'best' },
        tiktok:  { label: 'TikTok (9:16) 1080×1920', format: 'best' },
        podcast: { label: 'Podcast (16:9) 1920×1080', format: 'best' }
    };
    const preset = presetMap[name];
    if (!preset) return;
    setStatusText(`Preset applied: ${preset.label}`);
};

// Settings toggle chevron
if (toggleSettingsBtn && settingsPopover) {
    toggleSettingsBtn.addEventListener('click', () => {
        const open = !settingsPopover.classList.contains('hidden');
        toggleSettingsBtn.classList.toggle('open', !open);
    });
}

// Status bar helper
function setStatusText(msg, type) {
    const el = document.getElementById('status-text');
    const dot = document.querySelector('.status-dot');
    if (!el) return;
    el.textContent = msg;
    if (dot) {
        dot.style.background = type === 'error' ? '#ef5350' : type === 'busy' ? '#ffa726' : '#4fc3f7';
        dot.style.boxShadow  = type === 'error' ? '0 0 6px #ef5350' : type === 'busy' ? '0 0 6px #ffa726' : '0 0 8px rgba(79,195,247,.8)';
    }
}

// Init status bar
setStatusText('Ready');

// ─── AI Transcript Cuts Suggestion ──────────────────────────────────────
function generateCutsViaAI() {
    const url = videoUrlInput.value.trim();
    if (!url) return;

    if (isServerless && aiEnabledOnServer === false) {
        alert("AI Smart Clips are disabled on this deployment. On Vercel, set USE_HF_INFERENCE_API=true (and optionally HF_TOKEN) in Project → Settings → Environment Variables to enable them.");
        return;
    }
    
    aiGenerateCutsBtn.disabled = true;
    aiGenerateCutsBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Analyzing...`;
    if (!document.getElementById('spin-keyframe')) {
        const s = document.createElement('style'); s.id='spin-keyframe';
        s.textContent = '@keyframes spin{to{transform:rotate(360deg)}}';
        document.head.appendChild(s);
    }
    
    fetch('/api/ai/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
    })
    .then(res => res.json())
    .then(data => {
        aiGenerateCutsBtn.disabled = false;
        aiGenerateCutsBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12,2 15.09,8.26 22,9.27 17,14.14 18.18,21.02 12,17.77 5.82,21.02 7,14.14 2,9.27 8.91,8.26"/></svg> Generate AI Cuts`;
        
        if (data.error) {
            alert(`AI Analysis failed: ${data.error}`);
            return;
        }
        
        if (!data.sections || data.sections.length === 0) {
            alert("No highlights or cuts could be automatically generated for this video.");
            return;
        }
        
        // Append generated clips to clipRegions
        data.sections.forEach((sec, idx) => {
            const newRegion = {
                id: Date.now() + Math.random(),
                start: parseFloat(sec.start.toFixed(2)),
                end: parseFloat(sec.end.toFixed(2)),
                label: sec.label.substring(0, 30) || `AI Suggestion`,
                enabled: true
            };
            clipRegions.push(newRegion);
        });
        
        activeRegionIndex = clipRegions.length - 1;
        updateRegionsTable();
        updateSelectionRangeDisplay();
        drawTimeline();
        alert(`Successfully auto-generated ${data.sections.length} clipping regions!`);
    })
    .catch(err => {
        aiGenerateCutsBtn.disabled = false;
        aiGenerateCutsBtn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12,2 15.09,8.26 22,9.27 17,14.14 18.18,21.02 12,17.77 5.82,21.02 7,14.14 2,9.27 8.91,8.26"/></svg> Generate AI Cuts`;
        alert("AI Service is temporarily unavailable.");
    });
}

// ─── Serverless (Vercel) Client-Side Queue ─────────────────────────────────
// On the serverless deployment there is no persistent backend process, so the
// download queue lives in the browser: we ask the API to resolve/prepare the
// media, stream it through the function with fetch() (real progress + cancel),
// and save it locally via the File System Access API (or a Blob fallback).

const clientTasks = {};
let clientTaskSeq = 0;
let clientJobChain = Promise.resolve();

function enqueueClientJob(runner) {
    clientJobChain = clientJobChain.then(runner).catch(err => {
        console.warn('Client queue job failed:', err);
    });
}

function addClientTask(partial) {
    const id = `web_${Date.now()}_${++clientTaskSeq}`;
    const task = {
        id: id,
        title: partial.title || 'Video',
        type: partial.type || 'download',
        format: partial.format || 'mp4',
        status: 'pending',
        progress: 0.0,
        speed: 'N/A',
        eta: 'pending...',
        error: null,
        thumbnail: partial.thumbnail || '',
        abort: null
    };
    clientTasks[id] = task;
    rerenderWholeQueue();
    refreshActiveQueueCount();
    return task;
}

function updateClientTask(task) {
    if (!clientTasks[task.id]) return;
    clientTasks[task.id] = task;
    updateQueueItemUI(task);
}

function fmtBytes(n) {
    if (!n || n < 0) return '0 MB';
    if (n >= 1024 * 1024 * 1024) return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    return (n / 1024).toFixed(0) + ' KB';
}

function fmtEta(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '…';
    const s = Math.round(seconds);
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    const mm = String(m % 60).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function triggerDirectDownload(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename; // ignored cross-origin, still set for same-origin
    document.body.appendChild(a);
    a.click();
    a.remove();
}

// Stream a fetch Response to disk (File System Access API) or Blob fallback.
async function saveResponseToDisk(resp, filename, onProgress, signal) {
    const total = parseInt(resp.headers.get('Content-Length') || '0', 10) || 0;
    const reader = resp.body.getReader();
    const t0 = performance.now();
    let loaded = 0;
    let lastUiTick = 0;
    let writable = null;
    const chunks = [];

    if (window.showSaveFilePicker) {
        try {
            const handle = await window.showSaveFilePicker({ suggestedName: filename });
            writable = await handle.createWritable();
        } catch (e) {
            if (e && e.name === 'AbortError') throw e;
            writable = null; // no user gesture / unsupported → blob fallback
        }
    }

    if (!writable && total > 600 * 1024 * 1024) {
        throw new Error('File too large to buffer in memory without a save dialog; use the direct download link.');
    }

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            loaded += value.length;
            if (writable) {
                await writable.write(value);
            } else {
                chunks.push(value);
            }
            const now = performance.now();
            if (now - lastUiTick > 250) {
                lastUiTick = now;
                const secs = (now - t0) / 1000;
                const bps = loaded / Math.max(secs, 0.001);
                onProgress(
                    loaded,
                    total,
                    fmtBytes(bps) + '/s',
                    total && bps ? fmtEta((total - loaded) / bps) : '…'
                );
            }
        }
        if (writable) {
            await writable.close();
        } else {
            const blob = new Blob(chunks);
            const objectUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = objectUrl;
            a.download = filename || 'download';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
        }
        onProgress(loaded, total || loaded, 'N/A', '00:00');
    } catch (e) {
        if (writable) { try { writable.close(); } catch (_) {} }
        throw e;
    }
}

async function runClientDownload(task, opts) {
    const controller = new AbortController();
    task.abort = controller;
    task.status = 'downloading';
    task.progress = 0;
    task.eta = 'resolving…';
    updateClientTask(task);
    setStatusText(`Resolving "${task.title}"…`, 'busy');

    let meta = null;
    try {
        const res = await fetch('/api/resolve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: opts.url, format: opts.format })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        meta = data;
        if (task.title === 'Video' || task.title === 'No Video Loaded') task.title = meta.title || task.title;
        task.eta = meta.needs_processing ? 'processing on server…' : 'starting…';
        updateClientTask(task);
        setStatusText(`Downloading "${meta.filename}"…`, 'busy');

        const dlUrl = meta.proxy_url + '&filename=' + encodeURIComponent(meta.filename);
        const resp = await fetch(dlUrl, { signal: controller.signal });
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try { const err = await resp.json(); if (err && err.error) msg = err.error; } catch (_) {}
            throw new Error(msg);
        }

        await saveResponseToDisk(resp, meta.filename, (loaded, total, speed, eta) => {
            task.progress = total ? Math.min(99.9, (loaded / total) * 100) : Math.min(95, task.progress + 0.5);
            task.speed = speed;
            task.eta = eta;
            updateClientTask(task);
        }, controller.signal);

        task.status = 'completed';
        task.progress = 100;
        task.speed = 'N/A';
        task.eta = '00:00';
        updateClientTask(task);
        setStatusText(`Saved ${meta.filename}`, 'ok');
    } catch (err) {
        if (controller.signal.aborted) {
            task.status = 'cancelled';
            task.eta = 'cancelled';
            setStatusText('Download cancelled', 'error');
        } else if (meta && meta.direct_url) {
            // Proxy path failed (e.g. function time limit) — hand off to a
            // direct browser download from the resolved media URL.
            triggerDirectDownload(meta.direct_url, meta.filename);
            task.status = 'completed';
            task.progress = 100;
            task.eta = 'browser direct';
            setStatusText('Handed off to browser direct download…', 'busy');
        } else {
            const msg = String(err.message || err);
            task.status = 'failed';
            task.error = /Failed to fetch|NetworkError|network/i.test(msg)
                ? 'Connection to the server cut off (likely the function time/size limit). Try a smaller format, or use the self-hosted deployment for very large files.'
                : msg;
            setStatusText(`Download failed: ${task.error}`, 'error');
        }
        updateClientTask(task);
    }
}

async function runClientClip(task, opts) {
    const controller = new AbortController();
    task.abort = controller;
    task.status = 'downloading';
    task.progress = 0;
    task.eta = 'processing on server…';
    updateClientTask(task);
    setStatusText(`Cutting clip "${task.title}"…`, 'busy');

    // Server cuts the section first (yt-dlp --download-sections + ffmpeg);
    // show slow crawl progress while that happens.
    const pseudoTicker = setInterval(() => {
        if (task.status !== 'downloading') return;
        task.progress = Math.min(85, (task.progress || 0) + 0.6);
        task.speed = 'cutting…';
        updateClientTask(task);
    }, 900);

    try {
        const params = new URLSearchParams({
            url: opts.url,
            start: String(opts.start),
            end: String(opts.end),
            label: opts.label || 'clip',
            title: opts.title || 'video'
        });
        const resp = await fetch('/api/clip?' + params.toString(), { signal: controller.signal });
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try { const err = await resp.json(); if (err && err.error) msg = err.error; } catch (_) {}
            throw new Error(msg);
        }

        clearInterval(pseudoTicker);
        task.progress = 86;
        task.eta = 'transferring…';
        updateClientTask(task);

        let filename = `${(opts.title || 'video').replace(/[\\/:*?"<>|]/g, '').trim()}_${(opts.label || 'clip').replace(/[\\/:*?"<>|]/g, '').trim()}.mp4`;
        const cd = resp.headers.get('Content-Disposition') || '';
        const utf8Match = cd.match(/filename\*=UTF-8''([^;]+)/);
        const asciiMatch = cd.match(/filename="([^"]+)"/);
        if (utf8Match) {
            try { filename = decodeURIComponent(utf8Match[1]); } catch (_) {}
        } else if (asciiMatch) {
            filename = asciiMatch[1];
        }

        await saveResponseToDisk(resp, filename, (loaded, total, speed, eta) => {
            task.progress = 86 + Math.min(13.9, total ? (loaded / total) * 13.9 : 8);
            task.speed = speed;
            task.eta = eta;
            updateClientTask(task);
        }, controller.signal);

        task.status = 'completed';
        task.progress = 100;
        task.speed = 'N/A';
        task.eta = '00:00';
        updateClientTask(task);
        setStatusText(`Saved ${filename}`, 'ok');
    } catch (err) {
        clearInterval(pseudoTicker);
        if (controller.signal.aborted) {
            task.status = 'cancelled';
            task.eta = 'cancelled';
            setStatusText('Clip cancelled', 'error');
        } else {
            task.status = 'failed';
            task.error = String(err.message || err);
            setStatusText(`Clip failed: ${task.error}`, 'error');
        }
        updateClientTask(task);
    }
}
