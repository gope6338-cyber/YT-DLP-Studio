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

// Set Default Save Directory on Load
saveDirInput.value = appConfig.defaultSaveDir || "C:\\Users\\LCS\\Downloads";

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

// SSE Streaming for Queue
const sse = new EventSource('/api/stream');
sse.onmessage = function(event) {
    const msg = JSON.parse(event.data);
    if (msg.type === 'init') {
        renderQueue(msg.data);
    } else if (msg.type === 'task_update') {
        updateQueueItemUI(msg.data);
    }
};

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

function updateQueueItemUI(task) {
    if (task.status === 'removed') {
        const card = document.getElementById(`task-card-${task.id}`);
        if (card) card.remove();
        fetch('/api/queue')
            .then(res => res.json())
            .then(renderQueue);
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
        
        // Update active downloads count
        fetch('/api/queue')
            .then(res => res.json())
            .then(tasks => {
                const activeCount = tasks.filter(t => ['pending', 'downloading', 'clipping'].includes(t.status)).length;
                document.getElementById('active-download-count').innerText = `${activeCount} active`;
            });
    } else {
        // Refresh entire list if it's a new task
        fetch('/api/queue')
            .then(res => res.json())
            .then(renderQueue);
    }
}

window.cancelTask = function(taskId) {
    fetch('/api/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId })
    });
};

window.removeTask = function(taskId) {
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
        saveDirInput.value = data.default_save_dir;
        
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
        
        ctx.fillStyle = region.enabled ? 'rgba(255, 107, 0, 0.15)' : 'rgba(92, 104, 122, 0.1)';
        ctx.fillRect(xStart, 20, xEnd - xStart, 45);
        
        ctx.strokeStyle = region.enabled ? (isActive ? '#ff8533' : '#ff6b00') : '#5c687a';
        ctx.lineWidth = isActive ? 3 : 2;
        ctx.strokeRect(xStart, 20, xEnd - xStart, 45);
        
        // Draw boundaries white drag lines
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(xStart - 1, 20, 3, 45);
        ctx.fillRect(xEnd - 2, 20, 3, 45);
        
        // Draw black handles timestamps above region boundaries
        if (isActive || region.enabled) {
            ctx.fillStyle = '#ff6b00';
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
        ctx.fillStyle = '#ff6b00';
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
    ctx.strokeStyle = '#e74c3c';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(playheadX, 0);
    ctx.lineTo(playheadX, h);
    ctx.stroke();
    
    ctx.fillStyle = '#e74c3c';
    ctx.beginPath();
    ctx.arc(playheadX, 8, 4, 0, Math.PI * 2);
    ctx.fill();
}

function drawAudioWaveform(ctx, w, h) {
    ctx.strokeStyle = 'rgba(28, 32, 45, 0.4)';
    ctx.lineWidth = 1;
    const midY = h / 2 + 10;
    
    // Render a consistent waveform drawing
    for (let x = 0; x < w; x += 3) {
        const amp = (Math.sin(x * 0.03) * 0.35 + Math.sin(x * 0.08) * 0.25 + Math.cos(x * 0.005) * 0.4);
        const height = Math.abs(amp) * 22;
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
    
    const saveDir = saveDirInput.value.trim();
    const format = formatPresetSelect.value;
    const title = activeVideoTitle.innerText;
    
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
    
    const saveDir = saveDirInput.value.trim();
    const title = activeVideoTitle.innerText;
    
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
        dot.style.background = type === 'error' ? 'var(--c-red)' : type === 'busy' ? 'var(--c-orange)' : 'var(--c-green)';
        dot.style.boxShadow  = type === 'error' ? '0 0 6px var(--c-red)' : type === 'busy' ? '0 0 6px var(--c-orange)' : '0 0 6px var(--c-green)';
    }
}

// Init status bar
setStatusText('Ready');

// ─── AI Transcript Cuts Suggestion ──────────────────────────────────────
function generateCutsViaAI() {
    const url = videoUrlInput.value.trim();
    if (!url) return;
    
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
