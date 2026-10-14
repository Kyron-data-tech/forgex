// ForgeX - Core Application JavaScript

export function getCsrfToken() {
  const match = document.cookie.match(new RegExp('(^| )csrftoken=([^;]+)'));
  return match ? match[2] : '';
}

export async function fetchWithCsrf(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = options.headers || {};
  
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
    headers['X-CSRF-Token'] = getCsrfToken();
  }
  options.headers = headers;
  return fetch(url, options);
}

export function showToast(message, type = 'info') {
  const existing = document.getElementById('toast-container');
  if (existing) existing.remove();

  const container = document.createElement('div');
  container.id = 'toast-container';
  container.style.cssText = `
    position: fixed;
    top: 1.5rem;
    right: 1.5rem;
    background: ${type === 'error' ? '#ef4444' : type === 'success' ? '#10b981' : '#1e293b'};
    color: #fff;
    padding: 0.85rem 1.25rem;
    border-radius: 8px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.5);
    z-index: 9999;
    font-weight: 600;
    font-size: 0.9rem;
    border: 1px solid rgba(255,255,255,0.1);
    animation: fadeIn 0.3s ease;
  `;
  container.innerText = message;
  document.body.appendChild(container);
  setTimeout(() => container.remove(), 4000);
}

// Settings Page Logic
export function initSettingsPage() {
  // ── Storage section ────────────────────────────────────────────────────
  const pathInput = document.getElementById('storage-path-input');
  const btnValidate = document.getElementById('btn-validate-storage');
  const btnSaveStorage = document.getElementById('btn-save-storage');
  const statusDiv = document.getElementById('storage-status-report');

  if (btnValidate && pathInput) {
    btnValidate.addEventListener('click', async () => {
      const val = pathInput.value.trim();
      if (!val) return;
      btnValidate.disabled = true;
      btnValidate.innerText = 'Checking…';
      try {
        const res = await fetchWithCsrf('/api/settings/storage/validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: val })
        });
        const data = await res.json();
        if (res.ok) {
          statusDiv.innerHTML = `
            <div style="margin-top:0.75rem; padding:0.75rem; background:${data.sufficient ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)'}; border-radius:6px;">
              <p><strong>Path:</strong> ${data.path}</p>
              <p><strong>Free Disk:</strong> ${data.free_gb} GB (min: ${data.min_required_gb} GB)</p>
              <p><strong>Status:</strong> ${data.sufficient ? '✅ Sufficient &amp; Writable' : '❌ Insufficient space'}</p>
            </div>`;
          btnSaveStorage.disabled = !data.sufficient;
        } else {
          showToast(data.detail || 'Validation failed', 'error');
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        btnValidate.disabled = false;
        btnValidate.innerText = 'Validate';
      }
    });

    btnSaveStorage.addEventListener('click', async () => {
      const val = pathInput.value.trim();
      try {
        const res = await fetchWithCsrf('/api/settings/storage/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: val })
        });
        const data = await res.json();
        if (res.ok) showToast('Storage path saved!', 'success');
        else showToast(data.detail || 'Save failed', 'error');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  }

  // ── Credentials + headless login section ──────────────────────────────
  const loginStateBadge = document.getElementById('login-state-badge');
  const btnRefresh = document.getElementById('btn-refresh-status');
  const emailInput = document.getElementById('wh-email');
  const passwordInput = document.getElementById('wh-password');
  const btnTogglePw = document.getElementById('btn-toggle-pw');
  const btnSaveCreds = document.getElementById('btn-save-creds');
  const btnAutoLogin = document.getElementById('btn-auto-login');
  const btnClearCreds = document.getElementById('btn-clear-creds');
  const loginMsg = document.getElementById('login-status-msg');

  async function checkLoginStatus() {
    try {
      const res = await fetch('/api/settings/browser/status');
      const data = await res.json();
      if (loginStateBadge) {
        if (data.is_logged_in) {
          loginStateBadge.className = 'badge badge-published';
          loginStateBadge.innerText = '✓ Logged In';
        } else {
          loginStateBadge.className = 'badge badge-draft';
          loginStateBadge.innerText = 'Not Logged In';
        }
      }
      // If credentials are saved, enable the login button
      if (btnAutoLogin) {
        btnAutoLogin.disabled = !data.has_saved_credentials;
      }
    } catch (e) {}
  }

  // Password toggle
  if (btnTogglePw && passwordInput) {
    btnTogglePw.addEventListener('click', () => {
      const isHidden = passwordInput.type === 'password';
      passwordInput.type = isHidden ? 'text' : 'password';
      btnTogglePw.textContent = isHidden ? '🙈' : '👁';
    });
  }

  // Save credentials
  if (btnSaveCreds) {
    btnSaveCreds.addEventListener('click', async () => {
      const email = emailInput ? emailInput.value.trim() : '';
      const password = passwordInput ? passwordInput.value : '';
      if (!email || !password) {
        showToast('Enter both email and password.', 'error');
        return;
      }
      btnSaveCreds.disabled = true;
      btnSaveCreds.innerText = 'Saving…';
      try {
        const res = await fetchWithCsrf('/api/settings/browser/save-credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password })
        });
        const text = await res.text();
        let data;
        try { data = JSON.parse(text); } catch { data = { error: text || 'Server response error' }; }

        if (res.ok && data.success) {
          showToast('Credentials saved! Click "Login Now" to authenticate.', 'success');
          if (loginMsg) loginMsg.innerHTML = '<span style="color:#10b981;">✓ Credentials saved to .env</span>';
          if (btnAutoLogin) btnAutoLogin.disabled = false;
        } else {
          showToast(data.detail || data.error || 'Failed to save credentials', 'error');
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        btnSaveCreds.disabled = false;
        btnSaveCreds.innerText = '💾 Save Credentials';
      }
    });
  }

  // Headless auto-login
  if (btnAutoLogin) {
    btnAutoLogin.addEventListener('click', async () => {
      // If user typed email & password, auto-save first so Login Now works directly in 1 click
      const typedEmail = emailInput ? emailInput.value.trim() : '';
      const typedPass = passwordInput ? passwordInput.value : '';
      if (typedEmail && typedPass) {
        try {
          await fetchWithCsrf('/api/settings/browser/save-credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: typedEmail, password: typedPass })
          });
        } catch (_) {}
      }

      btnAutoLogin.disabled = true;
      btnAutoLogin.innerText = '🔄 Logging in silently…';
      if (loginMsg) loginMsg.innerHTML = '<span style="color:#6366f1;">🔄 Playwright is logging in headlessly — no window will open…</span>';
      try {
        const res = await fetchWithCsrf('/api/settings/browser/auto-login', { method: 'POST' });
        const text = await res.text();
        let data;
        try { data = JSON.parse(text); } catch { data = { error: text || `Server error (${res.status})` }; }

        if (res.ok && data.success) {
          showToast(data.already_logged_in ? 'Session already active!' : '✅ Login successful! Session saved.', 'success');
          if (loginMsg) loginMsg.innerHTML = '<span style="color:#10b981;">✓ Logged in — session stored in browser profile</span>';
          await checkLoginStatus();
        } else {
          const err = data.error || data.detail || 'Login failed';
          showToast('❌ ' + err, 'error');
          if (loginMsg) loginMsg.innerHTML = `<span style="color:#ef4444;">❌ ${err}</span>`;
        }
      } catch (err) {
        showToast('Login error: ' + err.message, 'error');
        if (loginMsg) loginMsg.innerHTML = `<span style="color:#ef4444;">❌ ${err.message}</span>`;
      } finally {
        btnAutoLogin.disabled = false;
        btnAutoLogin.innerText = '🚀 Login Now (Headless)';
      }
    });
  }

  // Clear credentials
  if (btnClearCreds) {
    btnClearCreds.addEventListener('click', async () => {
      if (!confirm('Clear saved credentials from .env?')) return;
      try {
        const res = await fetchWithCsrf('/api/settings/browser/clear-credentials', { method: 'DELETE' });
        const data = await res.json();
        showToast(data.message || 'Credentials cleared.', 'info');
        if (loginMsg) loginMsg.innerHTML = '<span style="color:#94a3b8;">Credentials cleared.</span>';
        if (btnAutoLogin) btnAutoLogin.disabled = true;
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  }

  // Refresh badge
  if (btnRefresh) {
    btnRefresh.addEventListener('click', async () => {
      await checkLoginStatus();
      showToast('Status refreshed.', 'info');
    });
  }

  checkLoginStatus();

  // ── Admin credentials section ─────────────────────────────────────────
  const adminModeBadge = document.getElementById('admin-mode-badge');
  const adminCredsBadge = document.getElementById('admin-creds-badge');
  const btnRefreshAdmin = document.getElementById('btn-refresh-admin-status');
  const adminUserInput = document.getElementById('wh-admin-username');
  const adminPassInput = document.getElementById('wh-admin-password');
  const btnToggleAdminPw = document.getElementById('btn-toggle-admin-pw');
  const btnSaveAdminCreds = document.getElementById('btn-save-admin-creds');
  const btnClearAdminCreds = document.getElementById('btn-clear-admin-creds');
  const adminStatusMsg = document.getElementById('admin-status-msg');

  const adminConfirmBanner = document.getElementById('admin-confirmation-banner');
  const adminErrorBanner = document.getElementById('admin-error-banner');

  async function checkAdminStatus() {
    try {
      const res = await fetch('/api/settings/admin/status');
      const data = await res.json();
      if (adminModeBadge) {
        adminModeBadge.className = data.admin_mode ? 'badge badge-published' : 'badge badge-draft';
        adminModeBadge.innerText = data.admin_mode ? 'Active (Direct Publish)' : 'Disabled';
      }
      if (adminCredsBadge) {
        if (data.has_saved_credentials) {
          adminCredsBadge.className = 'badge badge-published';
          adminCredsBadge.innerText = '✓ Saved (' + (data.admin_username || 'admin') + ')';
        } else {
          adminCredsBadge.className = 'badge badge-draft';
          adminCredsBadge.innerText = 'Not Saved';
        }
      }
      if (adminUserInput && data.admin_username && !adminUserInput.value) {
        adminUserInput.value = data.admin_username;
      }
    } catch (e) {}
  }

  if (btnToggleAdminPw && adminPassInput) {
    btnToggleAdminPw.addEventListener('click', () => {
      const isHidden = adminPassInput.type === 'password';
      adminPassInput.type = isHidden ? 'text' : 'password';
      btnToggleAdminPw.textContent = isHidden ? '🙈' : '👁';
    });
  }

  if (btnSaveAdminCreds) {
    btnSaveAdminCreds.addEventListener('click', async () => {
      const username = adminUserInput ? adminUserInput.value.trim() : '';
      const password = adminPassInput ? adminPassInput.value : '';
      if (!username || !password) {
        if (adminErrorBanner) {
          adminErrorBanner.innerHTML = '⚠️ Please enter both Admin Username and Admin Password.';
          adminErrorBanner.style.display = 'block';
        }
        if (adminConfirmBanner) adminConfirmBanner.style.display = 'none';
        showToast('Enter both admin username and password.', 'error');
        return;
      }

      if (adminErrorBanner) adminErrorBanner.style.display = 'none';
      if (adminConfirmBanner) adminConfirmBanner.style.display = 'none';
      btnSaveAdminCreds.disabled = true;
      btnSaveAdminCreds.innerText = 'Saving…';

      try {
        const res = await fetchWithCsrf('/api/settings/admin/save-credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, admin_mode: true })
        });
        const text = await res.text();
        let data;
        try {
          data = JSON.parse(text);
        } catch {
          data = { error: text || 'Server response error' };
        }

        if (res.ok && data.success) {
          showToast('✅ Admin credentials saved! Direct publishing active.', 'success');
          if (adminConfirmBanner) {
            adminConfirmBanner.innerHTML = `✅ <strong>Admin Credentials Saved Successfully!</strong><br>Logged in as <code>${username}</code>. ForgeX will now directly publish live Hero, Blog, and Story entries via the Admin Panel.`;
            adminConfirmBanner.style.display = 'block';
          }
          if (adminErrorBanner) adminErrorBanner.style.display = 'none';
          if (adminStatusMsg) adminStatusMsg.innerHTML = '<span style="color:#10b981;">✓ Admin credentials active — direct publishing enabled.</span>';
          if (adminCredsBadge) {
            adminCredsBadge.className = 'badge badge-published';
            adminCredsBadge.innerText = '✓ Saved (' + username + ')';
          }
          await checkAdminStatus();
        } else {
          const errMsg = data.detail || data.error || 'Failed to save admin credentials';
          showToast(errMsg, 'error');
          if (adminErrorBanner) {
            adminErrorBanner.innerHTML = `❌ <strong>Failed to save credentials:</strong> ${errMsg}`;
            adminErrorBanner.style.display = 'block';
          }
          if (adminConfirmBanner) adminConfirmBanner.style.display = 'none';
        }
      } catch (err) {
        showToast(err.message, 'error');
        if (adminErrorBanner) {
          adminErrorBanner.innerHTML = `❌ <strong>Network / Connection Error:</strong> ${err.message}. Please verify the ForgeX server is running.`;
          adminErrorBanner.style.display = 'block';
        }
        if (adminConfirmBanner) adminConfirmBanner.style.display = 'none';
      } finally {
        btnSaveAdminCreds.disabled = false;
        btnSaveAdminCreds.innerText = '💾 Save Admin Credentials';
      }
    });
  }

  if (btnClearAdminCreds) {
    btnClearAdminCreds.addEventListener('click', async () => {
      if (!confirm('Clear WorldHeroes admin credentials?')) return;
      try {
        const res = await fetchWithCsrf('/api/settings/admin/clear-credentials', { method: 'DELETE' });
        if (res.ok) {
          if (adminUserInput) adminUserInput.value = '';
          if (adminPassInput) adminPassInput.value = '';
          showToast('Admin credentials cleared.', 'info');
          if (adminConfirmBanner) adminConfirmBanner.style.display = 'none';
          if (adminErrorBanner) adminErrorBanner.style.display = 'none';
          if (adminStatusMsg) adminStatusMsg.innerHTML = '<span style="color:#94a3b8;">Admin credentials removed.</span>';
          await checkAdminStatus();
        }
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  }

  if (btnRefreshAdmin) {
    btnRefreshAdmin.addEventListener('click', async () => {
      await checkAdminStatus();
      showToast('Admin status refreshed.', 'info');
    });
  }

  checkAdminStatus();
}

// New Job Page Logic
export function initNewJobPage() {
  const form = document.getElementById('new-job-form');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('btn-submit-job');
    btn.disabled = true;
    btn.innerText = 'Researching & Preparing... (may take 5-10s)';

    const payload = {
      workflow_name: form.workflow_name.value,
      content_type: form.content_type.value,
      subject_name: form.subject_name.value.trim(),
      work_date: form.work_date.value,
      notes: form.notes ? form.notes.value.trim() : ''
    };

    try {
      const res = await fetchWithCsrf('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok) {
        if (data.duplicate) {
          if (confirm(data.message + '\n\nWould you like to open the existing job review?')) {
            window.location.href = `/jobs/${data.job_id}/review`;
            return;
          }
        } else {
          showToast('Job created & researched! Opening review...', 'success');
          window.location.href = `/jobs/${data.job_id}/review`;
        }
      } else {
        showToast(data.detail || 'Failed to create job', 'error');
      }
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.innerText = 'Start Research & Prepare';
    }
  });
}

// Review Page Logic
export function initReviewPage(jobId) {
  const reviewForm = document.getElementById('review-form');
  const btnSaveReview = document.getElementById('btn-save-review');
  const btnDryRun = document.getElementById('btn-dry-run');
  const btnPublish = document.getElementById('btn-approve-publish');
  const candidateCards = document.querySelectorAll('.image-card');
  const uploadForm = document.getElementById('custom-upload-form');

  // Candidate selection
  candidateCards.forEach(card => {
    card.addEventListener('click', async () => {
      const index = parseInt(card.dataset.index);
      candidateCards.forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      showToast('Downloading and attaching image...', 'info');

      try {
        const res = await fetchWithCsrf(`/api/jobs/${jobId}/select-image`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_index: index })
        });
        const data = await res.json();
        if (res.ok) {
          showToast('Image saved & selected with sidecar metadata!', 'success');
          setTimeout(() => location.reload(), 1000);
        } else {
          showToast(data.detail || 'Image selection failed', 'error');
        }
      } catch (e) {
        showToast(e.message, 'error');
      }
    });
  });

  // Custom upload
  if (uploadForm) {
    uploadForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fileInput = document.getElementById('custom-file-input');
      if (!fileInput.files.length) return;

      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      fd.append('license_type', document.getElementById('custom-license-input').value);
      fd.append('author', document.getElementById('custom-author-input').value);

      showToast('Validating and uploading custom image...', 'info');
      try {
        const res = await fetchWithCsrf(`/api/jobs/${jobId}/upload-image`, {
          method: 'POST',
          body: fd
        });
        const data = await res.json();
        if (res.ok) {
          showToast('Custom image verified and attached!', 'success');
          candidateCards.forEach(c => c.classList.remove('selected'));
          setTimeout(() => location.reload(), 1000);
        } else {
          showToast(data.detail || 'Upload failed', 'error');
        }
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  }

  // Save fields
  if (btnSaveReview && reviewForm) {
    btnSaveReview.addEventListener('click', async () => {
      const fd = new FormData(reviewForm);
      const reviewData = Object.fromEntries(fd.entries());

      try {
        const res = await fetchWithCsrf(`/api/jobs/${jobId}/review`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ review_data: reviewData })
        });
        if (res.ok) {
          showToast('Review data saved!', 'success');
        }
      } catch (e) {
        showToast(e.message, 'error');
      }
    });
  }

  // Dry run
  if (btnDryRun) {
    btnDryRun.addEventListener('click', async () => {
      btnDryRun.disabled = true;
      btnDryRun.innerText = '🔄 Running Browser Dry-Run...';
      try {
        const res = await fetchWithCsrf(`/api/jobs/${jobId}/dry-run`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          showResultModal('Dry-Run Results', data, false);
          // Only enable publish if dry-run actually succeeded (form was filled)
          if (data.success && !data.needs_login) {
            btnPublish.disabled = false;
            showToast('Dry-run passed! You can now Approve & Publish.', 'success');
          } else if (data.needs_login) {
            showToast('⚠️ Not logged in — go to Settings → Open site and log in, then close the browser window.', 'error');
          }
        } else {
          showToast(data.detail || 'Dry run API error', 'error');
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        btnDryRun.disabled = false;
        btnDryRun.innerText = '🔍 Run Dry Run';
      }
    });
  }

  // Approve & Publish
  if (btnPublish) {
    btnPublish.addEventListener('click', async () => {
      if (!confirm('⚠️ Safety Gate\n\nThis will SUBMIT the nomination form to worldheros.com.\n\nAre you sure you want to publish?')) {
        return;
      }
      btnPublish.disabled = true;
      btnPublish.innerText = '🚀 Publishing to worldheros.com...';
      showToast('Submitting nomination form — please wait...', 'info');
      try {
        const res = await fetchWithCsrf(`/api/jobs/${jobId}/publish`, { method: 'POST' });
        const data = await res.json();
        showResultModal('Publish Results', data, true);
        if (res.ok && data.success) {
          showToast('✅ Nomination submitted successfully!', 'success');
          setTimeout(() => { window.location.href = '/history'; }, 3000);
        } else if (data.needs_login) {
          showToast('❌ Not logged in — go to Settings → Open site and log in', 'error');
          btnPublish.disabled = false;
          btnPublish.innerText = '🚀 Approve & Publish';
        } else {
          showToast('❌ Publish error: ' + (data.error || 'Check logs below'), 'error');
          btnPublish.disabled = false;
          btnPublish.innerText = '🚀 Approve & Publish';
        }
      } catch (e) {
        showToast('Publish error: ' + e.message, 'error');
        btnPublish.disabled = false;
        btnPublish.innerText = '🚀 Approve & Publish';
      }
    });
  }
}

function showResultModal(title, result, isPublish) {
  const existing = document.getElementById('result-modal');
  if (existing) existing.remove();

  const success = result.success;
  const needsLogin = result.needs_login;
  const logs = result.logs || [];
  const filledFields = result.filled_fields || {};
  const liveFields = result.live_fields_detected || [];
  
  let statusMsg = '';
  let statusColor = '';
  if (needsLogin) {
    statusMsg = '🔒 Not logged in — go to Settings → Open site and log in, then close the browser window before retrying.';
    statusColor = '#f59e0b';
  } else if (success) {
    statusMsg = isPublish 
      ? `✅ Nomination submitted! Post-submit URL: ${result.published_url || 'Pending review'}` 
      : '✅ Form filled in dry-run mode. Submit NOT clicked.';
    statusColor = '#10b981';
  } else {
    statusMsg = '❌ ' + (result.error || result.message || 'Operation failed — see logs below');
    statusColor = '#ef4444';
  }

  const fieldsHtml = Object.keys(filledFields).length > 0
    ? `<div style="margin-bottom:0.75rem;">
         <strong style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em;">Fields Filled</strong>
         <div style="display:flex; flex-wrap:wrap; gap:0.35rem; margin-top:0.4rem;">
           ${Object.entries(filledFields).map(([k,v]) => `<span class="badge badge-passed">${k}</span>`).join('')}
         </div>
       </div>`
    : '';

  const liveFieldsHtml = liveFields.length > 0
    ? `<div style="margin-bottom:0.75rem;">
         <strong style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em;">Live Form Fields Detected</strong>
         <div style="font-size:0.8rem; color:#64748b; margin-top:0.25rem;">${liveFields.join(', ') || '—'}</div>
       </div>`
    : '';

  const modal = document.createElement('div');
  modal.id = 'result-modal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-content" style="max-width:860px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem;">
        <h2>${isPublish ? '🚀 Publish Result' : '🔍 Dry-Run Result'}: ${title}</h2>
        <button class="btn btn-secondary btn-small" id="btn-close-modal">✕ Close</button>
      </div>
      <p style="margin-bottom:1rem; color:${statusColor}; font-weight:600; line-height:1.5;">${statusMsg}</p>
      ${fieldsHtml}
      ${liveFieldsHtml}
      ${result.screenshot_b64 ? `
        <div style="margin-bottom:1rem;">
          <strong style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em;">${isPublish ? 'Post-Submit Screenshot' : 'Form Screenshot'}</strong>
          <div style="border:1px solid #334155; border-radius:6px; overflow:hidden; max-height:400px; overflow-y:auto; margin-top:0.5rem;">
            <img src="data:image/png;base64,${result.screenshot_b64}" style="width:100%; display:block;" alt="Screenshot">
          </div>
        </div>
      ` : ''}
      <div>
        <strong style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em;">Automation Logs</strong>
        <div style="background:#0a0f1d; padding:0.75rem; border-radius:6px; font-family:monospace; font-size:0.8rem; max-height:200px; overflow-y:auto; margin-top:0.5rem; border:1px solid #1e293b;">
          ${logs.map(l => {
            const color = l.startsWith('✓') ? '#10b981' : l.startsWith('✗') || l.startsWith('❌') ? '#ef4444' : l.startsWith('⚠') ? '#f59e0b' : '#94a3b8';
            return `<div style="color:${color}; padding:0.1rem 0;">${l}</div>`;
          }).join('')}
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  document.getElementById('btn-close-modal').onclick = () => modal.remove();
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
}
