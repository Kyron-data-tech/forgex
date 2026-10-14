// ForgeX - Chat Controller (JavaScript + WebGPU)
import { fetchWithCsrf, showToast } from './app.js';

let chatWorker = null;
let isGpuSupported = false;
let isModelReady = false;
let conversationHistory = [];

export async function initChatAssistant() {
  const toggleBtn = document.getElementById('chat-toggle-btn');
  const drawer = document.getElementById('chat-drawer');
  const btnClose = document.getElementById('chat-close-btn');
  const btnUnload = document.getElementById('chat-unload-btn');
  const chatMessages = document.getElementById('chat-messages');
  const chatInput = document.getElementById('chat-input');
  const btnSend = document.getElementById('chat-send-btn');
  const gpuBadge = document.getElementById('chat-gpu-badge');
  const consentModal = document.getElementById('chat-consent-modal');
  const btnConsentConfirm = document.getElementById('btn-consent-confirm');
  const btnConsentCancel = document.getElementById('btn-consent-cancel');
  const progressContainer = document.getElementById('chat-progress-container');
  const progressBar = document.getElementById('chat-progress-bar');
  const progressText = document.getElementById('chat-progress-text');

  if (!toggleBtn || !drawer) return;

  // 1. Feature-detect WebGPU
  if (navigator.gpu) {
    try {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) {
        isGpuSupported = true;
        gpuBadge.innerText = 'WebGPU Ready';
        gpuBadge.style.color = '#10b981';
      } else {
        gpuBadge.innerText = 'GPU Unavailable (CPU/WASM)';
      }
    } catch (e) {
      gpuBadge.innerText = 'CPU/WASM Fallback';
    }
  } else {
    gpuBadge.innerText = 'WebGPU Unsupported (CPU/WASM)';
  }

  // Toggle drawer visibility
  toggleBtn.addEventListener('click', () => {
    drawer.classList.remove('minimized');
    toggleBtn.style.display = 'none';
    if (!chatWorker && !isModelReady) {
      // Show user consent modal before downloading model
      consentModal.style.display = 'flex';
    }
  });

  btnClose.addEventListener('click', () => {
    drawer.classList.add('minimized');
    toggleBtn.style.display = 'flex';
  });

  // Consent modal buttons
  btnConsentCancel.addEventListener('click', () => {
    consentModal.style.display = 'none';
    addAssistantMessage('AI Chat was declined. You can enable it anytime by clicking the Chat icon.');
  });

  btnConsentConfirm.addEventListener('click', () => {
    consentModal.style.display = 'none';
    startWorkerAndLoadModel();
  });

  function startWorkerAndLoadModel() {
    progressContainer.style.display = 'block';
    progressText.innerText = 'Initializing Web Worker...';

    // Start Worker
    chatWorker = new Worker('/static/js/chat.worker.js', { type: 'module' });

    chatWorker.onmessage = async (e) => {
      const { type, text, progress, message, error } = e.data;

      if (type === 'progress') {
        const pct = Math.round(progress * 100);
        progressBar.style.width = `${pct}%`;
        progressText.innerText = `${text} (${pct}%)`;
      } else if (type === 'init_done') {
        isModelReady = true;
        progressContainer.style.display = 'none';
        btnUnload.style.display = 'inline-block';
        addAssistantMessage(message || 'ForgeX Assistant ready. How can I help you today?');
      } else if (type === 'done') {
        btnSend.disabled = false;
        renderAssistantResponse(text);
      } else if (type === 'error') {
        btnSend.disabled = false;
        showToast('Chat Error: ' + error, 'error');
        addAssistantMessage('⚠️ An error occurred during inference: ' + error);
      } else if (type === 'unloaded') {
        isModelReady = false;
        btnUnload.style.display = 'none';
        showToast('GPU memory released.', 'info');
        addAssistantMessage('Model unloaded and GPU memory freed.');
      }
    };

    // Recommended small quantized model (~360M-500M)
    const selectedModel = isGpuSupported
      ? 'SmolLM2-360M-Instruct-q4f16_1-MLC'
      : 'fallback';

    chatWorker.postMessage({
      type: 'init_model',
      payload: { modelName: selectedModel, useWebGPU: isGpuSupported }
    });
  }

  // Unload Model
  btnUnload.addEventListener('click', () => {
    if (chatWorker) {
      chatWorker.postMessage({ type: 'unload' });
    }
  });

  // Send message
  btnSend.addEventListener('click', sendMessage);
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  function sendMessage() {
    const text = chatInput.value.trim();
    if (!text) return;
    if (!chatWorker || !isModelReady) {
      consentModal.style.display = 'flex';
      return;
    }

    addUserMessage(text);
    chatInput.value = '';
    btnSend.disabled = true;

    conversationHistory.push({ role: 'user', content: text });

    chatWorker.postMessage({
      type: 'generate',
      payload: {
        messages: conversationHistory.slice(-6), // Keep context bounded to prevent VRAM overflow
        userPrompt: text
      }
    });
  }

  function addUserMessage(msg) {
    const div = document.createElement('div');
    div.className = 'chat-message chat-user';
    div.innerText = msg;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function addAssistantMessage(msg) {
    const div = document.createElement('div');
    div.className = 'chat-message chat-assistant';
    div.innerHTML = formatMarkdown(msg);
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function renderAssistantResponse(rawReply) {
    conversationHistory.push({ role: 'assistant', content: rawReply });

    // Detect allowlisted tool actions inside ```action ``` block
    const actionMatch = rawReply.match(/```action\s*([\s\S]*?)\s*```/);
    let cleanReply = rawReply.replace(/```action[\s\S]*?```/g, '').trim();

    const container = document.createElement('div');
    container.className = 'chat-message chat-assistant';
    container.innerHTML = formatMarkdown(cleanReply);

    if (actionMatch) {
      try {
        const actionObj = JSON.parse(actionMatch[1]);
        const actionBtn = document.createElement('button');
        actionBtn.className = 'btn btn-primary btn-small';
        actionBtn.style.marginTop = '0.5rem';
        actionBtn.innerText = `⚡ Execute: ${actionObj.action}`;
        actionBtn.onclick = () => executeAllowlistedTool(actionObj);
        container.appendChild(actionBtn);
      } catch (e) {
        console.warn('Failed to parse tool action:', e);
      }
    }

    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  async function executeAllowlistedTool(actionObj) {
    showToast(`Executing ${actionObj.action}...`, 'info');
    try {
      const res = await fetchWithCsrf('/api/chat/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(actionObj)
      });
      const data = await res.json();
      if (res.ok) {
        showToast('Action completed!', 'success');
        if (actionObj.action === 'create_job' && data.job_id) {
          window.location.href = `/jobs/${data.job_id}/review`;
        } else {
          addAssistantMessage(`Result from **${actionObj.action}**:\n\`\`\`json\n${JSON.stringify(data, null, 2)}\n\`\`\``);
        }
      } else {
        showToast(data.detail || 'Action failed', 'error');
        addAssistantMessage(`❌ Action error: ${data.detail || 'Execution failed'}`);
      }
    } catch (e) {
      showToast(e.message, 'error');
    }
  }

  function formatMarkdown(text) {
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code style="background:#334155; padding:2px 4px; border-radius:3px;">$1</code>')
      .replace(/\n/g, '<br>');
  }
}
