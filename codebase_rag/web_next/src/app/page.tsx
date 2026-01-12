"use client";
import React, { useState, useEffect } from 'react';
import { useAuth } from '@/context/AuthContext';
import { LogOut, Play, ShieldAlert, GitPullRequest, Terminal } from 'lucide-react';

export default function Dashboard() {
  const { token, logout } = useAuth();
  const [prUrl, setPrUrl] = useState('');
  const [reviewMode, setReviewMode] = useState('auto'); // New State
  const [activeTab, setActiveTab] = useState<'review' | 'logs'>('review');
  const [isLoading, setIsLoading] = useState(false);
  const [reviewText, setReviewText] = useState('');
  const [logs, setLogs] = useState<any[]>([]);

  // Polling logs
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (activeTab === 'logs') {
      const fetchLogs = async () => {
        try {
          const res = await fetch('/api/logs');
          const data = await res.json();
          if (data.logs) setLogs(data.logs);
        } catch (e) { console.error("Log poll error", e); }
      };
      fetchLogs();
      interval = setInterval(fetchLogs, 2000);
    }
    return () => clearInterval(interval);
  }, [activeTab]);

  const handleAnalyze = async () => {
    if (!prUrl) return;
    setIsLoading(true);
    try {
      // Parse the URL to extract repo details
      const urlParts = prUrl.match(/github\.com\/([^\/]+)\/([^\/]+)\/pull\/(\d+)/);
      if (!urlParts) {
        throw new Error("Invalid PR URL. Must be: https://github.com/org/repo/pull/123");
      }
      const [_, org, repo, prNum] = urlParts;
      const repoUrl = `https://github.com/${org}/${repo}.git`;

      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          github_token: token,
          repo_url: repoUrl,
          pr_number: parseInt(prNum),
          project_name: "TUTORIAL_RAG",
          mode: reviewMode // Pass Selected Mode
        })
      });

      const contentType = res.headers.get("content-type");
      if (!contentType || !contentType.includes("application/json")) {
        const text = await res.text();
        throw new Error(`Server Error (${res.status}): ${text}`);
      }

      const data = await res.json();
      if (res.status === 401) { logout(); throw new Error("Session Expired"); }
      if (data.status === 'error') { throw new Error(data.message); }

      setReviewText(typeof data.review === 'string' ? data.review : JSON.stringify(data.review, null, 2));
      setActiveTab('review');

    } catch (e: any) {
      console.error(e);
      alert(`Error: ${e.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="container">
      {/* Header */}
      <div className="dashboard-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <span style={{ fontSize: '1.5rem' }}>🔮</span>
          <h2 style={{ fontSize: '1.2rem', fontWeight: 600 }}>PocketFlow Dashboard</h2>
        </div>
        <button onClick={logout} className="btn-action" style={{ background: 'rgba(255,255,255,0.1)', color: 'white' }}>
          <LogOut size={16} style={{ marginRight: 8 }} /> Logout
        </button>
      </div>

      {/* Input Area */}
      <div className="top-nav">
        <div className="pr-input-container">
          <input
            value={prUrl}
            onChange={(e) => setPrUrl(e.target.value)}
            placeholder="Paste PR URL (e.g., https://github.com/org/repo/pull/123)"
          />

          <select
            value={reviewMode}
            onChange={(e) => setReviewMode(e.target.value)}
            className="mode-select"
            style={{
              background: 'rgba(0,0,0,0.3)',
              color: '#fff',
              border: '1px solid rgba(255,255,255,0.1)',
              padding: '0.8rem',
              borderRadius: '8px',
              cursor: 'pointer'
            }}
          >
            <option value="auto">⚡ Auto (Budget vs Brains)</option>
            <option value="fast">🎯 Tier 1: Sniper (Fast)</option>
            <option value="standard">🧐 Tier 2: Reviewer (Standard)</option>
            <option value="security">🛡️ Tier 3: Council (Security)</option>
            <option value="deep_debug">🌊 Tier 4: Deep Dive (Debug)</option>
          </select>

          <button onClick={handleAnalyze} disabled={isLoading} className="btn-primary" style={{ width: 'auto', whiteSpace: 'nowrap' }}>
            {isLoading ? 'Analyzing...' : <><Play size={16} style={{ display: 'inline', marginRight: 6 }} /> Run Cognit Protocol</>}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="tabs">
        <button className={`tab-btn ${activeTab === 'review' ? 'active' : ''}`} onClick={() => setActiveTab('review')}>
          <ShieldAlert size={16} style={{ display: 'inline', marginRight: 6, verticalAlign: 'text-bottom' }} />
          Protocol Review
        </button>
        <button className={`tab-btn ${activeTab === 'logs' ? 'active' : ''}`} onClick={() => setActiveTab('logs')}>
          <Terminal size={16} style={{ display: 'inline', marginRight: 6, verticalAlign: 'text-bottom' }} />
          System Logs
        </button>
      </div>

      {/* Content */}
      <div className="tab-content">
        {activeTab === 'review' && (
          <div className="markdown-body" style={{ color: '#e6edf3', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
            {reviewText ? (
              <div className="glass-card" style={{ textAlign: 'left', maxWidth: '100%' }}>
                <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{reviewText}</pre>
              </div>
            ) : (
              <div className="placeholder-state">
                <GitPullRequest size={48} style={{ opacity: 0.3, marginBottom: '1rem' }} />
                <p>Ready to analyze. Enter a PR URL above to initiate the Cognit Efficiency Protocol.</p>
              </div>
            )}
          </div>
        )}

        {activeTab === 'logs' && (
          <div className="logs-container">
            <div className="logs-header">
              <span>Backend Event Stream</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: '0.7rem', color: '#666' }}>LIVE</span>
                <span className="status-dot"></span>
              </div>
            </div>
            <div className="terminal-body" id="term-body">
              {logs.length > 0 ? logs.map((l, i) => (
                <span key={i} className="log-line">
                  <span className="log-time">[{l.timestamp}]</span>
                  <span className={`log-lvl ${l.level}`}> {l.level}</span> {l.message}
                </span>
              )) : <span style={{ opacity: 0.5 }}>Waiting for logs...</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
