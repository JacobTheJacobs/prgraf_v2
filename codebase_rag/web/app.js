document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const loginView = document.getElementById('login-view');
    const dashboardView = document.getElementById('dashboard-view');
    const loginForm = document.getElementById('login-form');
    const githubTokenInput = document.getElementById('github-token');
    const logoutBtn = document.getElementById('logout-btn');
    const analyzeBtn = document.getElementById('analyze-btn');
    const contentArea = document.getElementById('content-area');
    const tabBtns = document.querySelectorAll('.tab-btn');

    // State
    let token = localStorage.getItem('gh_token');

    // Init
    if (token) {
        showDashboard();
    } else {
        showLogin();
    }

    // --- Login Logic ---
    loginForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const inputToken = githubTokenInput.value.trim();

        if (inputToken.startsWith('gh')) {
            token = inputToken;
            localStorage.setItem('gh_token', token);
            showDashboard();
        } else {
            showError("Invalid Token Format (should start with 'gh...')");
        }
    });

    logoutBtn.addEventListener('click', () => {
        token = null;
        localStorage.removeItem('gh_token');
        showLogin();
    });

    // --- View Switching ---
    function showDashboard() {
        loginView.classList.remove('active');
        loginView.classList.add('hidden');

        dashboardView.classList.remove('hidden');
        // Small delay to allow display:block to apply before opacity transition
        setTimeout(() => {
            dashboardView.classList.add('active');
        }, 50);
    }

    function showLogin() {
        dashboardView.classList.remove('active');
        dashboardView.classList.add('hidden');

        loginView.classList.remove('hidden');
        setTimeout(() => {
            loginView.classList.add('active');
        }, 50);

        githubTokenInput.value = '';
    }

    function showError(msg) {
        const status = document.getElementById('login-status');
        status.textContent = msg;
        status.style.color = '#ef4444';
        setTimeout(() => status.textContent = '', 3000);
    }

    // --- Tabs Logic ---
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            // Remove active
            tabBtns.forEach(b => b.classList.remove('active'));
            // Add active
            btn.classList.add('active');

            // TODO: Switch Content
            const tabName = btn.dataset.tab;
            renderTabContent(tabName);
        });
    });

    // --- API Helper ---
    async function callApi(endpoint, body) {
        try {
            const resp = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(body)
            });
            const data = await resp.json();

            // Check for HTTP errors or explicit error status
            if (!resp.ok || data.status === 'error') {
                const msg = data.detail || data.message || 'API Error';

                // Auth Check: Redirect to login if authentication (or session) fails
                if (resp.status === 401 || resp.status === 403) {

                    token = null;
                    localStorage.removeItem('gh_token');
                    showLogin();
                    showError("Session Failed: " + msg);
                    throw new Error("Authentication Redirect");
                }

                throw new Error(msg);
            }
            return data;
        } catch (e) {
            throw e;
        }
    }

    // --- Analysis Logic ---
    analyzeBtn.addEventListener('click', async () => {
        const prUrl = document.getElementById('pr-url').value;
        if (!prUrl) return alert("Please enter a PR URL");

        contentArea.innerHTML = `
            <div class="placeholder-state">
                <div style="font-size: 2rem; margin-bottom: 1rem;">🔮</div>
                <p>Analyzing Code Graph & Blast Radius...</p>
                <div class="loading-bar"></div>
            </div>
        `;

        try {
            const result = await callApi('/api/analyze', {
                github_token: token,
                pr_url: prUrl,
                project_name: "TUTORIAL_RAG"
            });

            // Store result for review step
            window.lastAnalysis = result;
            renderTriage(result.triage);
            renderBlastRadius(result.blast_radius);

            // Switch to Triage tab immediately
            document.querySelector('[data-tab="triage"]').click();

        } catch (e) {
            contentArea.innerHTML = `<div class="error-msg">Analysis Failed: ${e.message}</div>`;
        }
    });

    // --- Rendering Logic ---
    function renderTriage(triageData) {
        // We attach data to the tab element or store gobally
        window.triageHtml = `
            <h3>Structural Triage</h3>
            <table class="data-table">
                <thead><tr><th>File</th><th>Category</th></tr></thead>
                <tbody>
                    ${triageData.map(item => `
                        <tr>
                            <td>${item.file}</td>
                            <td><span class="badge ${item.category}">${item.category}</span></td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;
    }

    function renderBlastRadius(blastData) {
        if (!blastData || blastData.length === 0) {
            window.blastHtml = `<div class="success-box">✅ Zero Blast Radius Detected</div>`;
            return;
        }
        window.blastHtml = `
            <h3>Graph Impact Analysis</h3>
            ${blastData.map(item => `
                <div class="impact-card">
                    <div class="card-header">
                        <span class="badg high">RISK: ${item.risk_level}</span>
                        <code>${item.function}</code>
                    </div>
                    <div class="callers-list">
                        <strong>Callers:</strong>
                        <ul>
                            ${item.callers.map(c => `<li>${c}</li>`).join('')}
                        </ul>
                    </div>
                </div>
            `).join('')}`;
    }

    // --- Tab Switching Update ---
    function renderTabContent(tabName) {
        if (tabName === 'triage') {
            contentArea.innerHTML = window.triageHtml || '<p>Run Analysis first.</p>';
        } else if (tabName === 'blast') {
            contentArea.innerHTML = window.blastHtml || '<p>Run Analysis first.</p>';
        } else if (tabName === 'review') {
            if (!window.lastAnalysis) {
                contentArea.innerHTML = '<p>Run Analysis first.</p>';
                return;
            }
            contentArea.innerHTML = `
                <div class="review-section">
                    <h3>Architect Review</h3>
                    <div class="review-actions">
                        <button id="gen-review-btn" class="btn-primary">Generate LLM Review</button>
                        <button id="post-comment-btn" class="btn-action" style="display:none; margin-left:10px;">Post to GitHub 🚀</button>
                    </div>
                    <div id="review-output" class="markdown-body"></div>
                </div>
            `;

            const genBtn = document.getElementById('gen-review-btn');
            const postBtn = document.getElementById('post-comment-btn');
            const outputDiv = document.getElementById('review-output');

            // Generate Logic
            genBtn.addEventListener('click', async (e) => {
                genBtn.disabled = true;
                genBtn.textContent = "Generating...";
                try {
                    const reviewResp = await callApi('/api/review', {
                        github_token: token,
                        pr_data: window.lastAnalysis
                    });

                    window.currentReview = reviewResp.review; // Store for posting
                    outputDiv.innerText = reviewResp.review;

                    // Show Post Button
                    postBtn.style.display = 'inline-block';
                    genBtn.textContent = "Regenerate";

                } catch (err) {
                    outputDiv.innerHTML = `<span style="color:red">Error: ${err.message}</span>`;
                    genBtn.textContent = "Generate LLM Review";
                }
                genBtn.disabled = false;
            });

            // Post Logic
            postBtn.addEventListener('click', async () => {
                if (!window.currentReview) return;

                const prUrl = document.getElementById('pr-url').value;
                postBtn.disabled = true;
                postBtn.textContent = "Posting...";

                try {
                    const resp = await callApi('/api/comment', {
                        github_token: token,
                        pr_url: prUrl,
                        body: window.currentReview
                    });

                    if (resp.status === 'success') {
                        alert("✅ Review Posted Successfully!");
                        postBtn.textContent = "Posted ✅";
                    } else {
                        alert("❌ Failed: " + resp.message);
                        postBtn.textContent = "Post to GitHub 🚀";
                        postBtn.disabled = false;
                    }
                } catch (e) {
                    alert("Error posting: " + e.message);
                    postBtn.disabled = false;
                }
            });
        } else if (tabName === 'logs') {
            startLogPolling();
        }
    }

    // --- Log Polling ---
    let logInterval = null;

    async function startLogPolling() {
        if (logInterval) clearInterval(logInterval);

        contentArea.innerHTML = `
            <div class="logs-container">
                <div class="logs-header">
                    <span>Server Logs</span>
                    <span class="status-dot"></span>
                </div>
                <pre id="logs-output" class="terminal-body">Loading stream...</pre>
            </div>
        `;

        const fetchLogs = async () => {
            try {
                const resp = await fetch('/api/logs');
                const data = await resp.json();
                const logs = data.logs || [];

                const logBody = document.getElementById('logs-output');
                if (!logBody) return; // Tab switched

                logBody.innerHTML = logs.map(l =>
                    `<span class="log-line"><span class="log-time">[${l.timestamp}]</span> <span class="log-lvl ${l.level}">${l.level}</span> ${l.message}</span>`
                ).join('\n');

                // Auto-scroll
                logBody.scrollTop = logBody.scrollHeight;

            } catch (e) {
                console.error("Log fetch failed", e);
            }
        };

        // Initial fetch
        fetchLogs();
        // Poll every 2s
        logInterval = setInterval(fetchLogs, 2000);
    }

    // Clear interval when switching tabs
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.tab !== 'logs' && logInterval) {
                clearInterval(logInterval);
                logInterval = null;
            }
        });
    });
});
