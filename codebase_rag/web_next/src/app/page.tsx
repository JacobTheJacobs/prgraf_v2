"use client";
import React, { useState, useEffect } from 'react';
import { useAuth } from '@/context/AuthContext';
import { LogOut, Play, ShieldAlert, GitPullRequest, Terminal, RefreshCcw, Copy } from 'lucide-react';

export default function Dashboard() {
  const { token, logout } = useAuth();
  const [prUrl, setPrUrl] = useState('');
  const [reviewMode, setReviewMode] = useState('sentinel');
  const [activeTab, setActiveTab] = useState<'review' | 'logs' | 'graph' | 'vectors'>('review');
  const [isLoading, setIsLoading] = useState(false);
  const [reviewText, setReviewText] = useState('');
  const [logs, setLogs] = useState<any[]>([]);
  const [projects, setProjects] = useState<string[]>([]);
  const [selectedProject, setSelectedProject] = useState('TUTORIAL_RAG');

  // Manual Log refresh only
  const fetchLogs = async () => {
    try {
      const res = await fetch('/api/logs');
      const data = await res.json();
      if (data.logs) setLogs(data.logs);
    } catch (e) { console.error("Log poll error", e); }
  };


  const handleCopyLogs = () => {
    const text = logs.map(l => `[${l.timestamp}] ${l.level} ${l.message}`).join('\n');
    navigator.clipboard.writeText(text).catch(err => console.error('Failed to copy logs:', err));
  };

  useEffect(() => {
    // Fetch Projects
    fetch('/api/projects')
      .then(res => res.json())
      .then(data => {
        if (data.projects) {
          setProjects(data.projects);
          if (data.projects.length > 0) setSelectedProject(data.projects[0]);
        }
      })
      .catch(console.error);
  }, []);

  useEffect(() => {
    if (activeTab === 'logs') fetchLogs();
  }, [activeTab]);

  const handleAnalyze = async () => {
    if (!prUrl) return;
    setIsLoading(true);
    setLogs([]); // Clear previous logs
    try {
      // Parse the URL to extract repo details
      const urlParts = prUrl.match(/github\.com\/([^\/]+)\/([^\/]+)\/pull\/(\d+)/);
      if (!urlParts) {
        throw new Error("Invalid PR URL. Must be: https://github.com/org/repo/pull/123");
      }
      const [_, org, repo, prNum] = urlParts;
      const repoUrl = `https://github.com/${org}/${repo}.git`;

      // Use repo name as project name if no project selected or default
      const derivedProject = repo;

      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          github_token: token,
          repo_url: repoUrl,
          pr_number: parseInt(prNum),
          project_name: selectedProject || derivedProject,
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

      // Handle Async Job Polling
      if (data.status === 'queued' && data.job_id) {
        let jobData: any = null;
        while (true) {
          // Poll every 2 seconds
          await new Promise(r => setTimeout(r, 2000));

          try {
            const pollRes = await fetch(`/api/jobs/${data.job_id}`);
            if (pollRes.status === 200) {
              jobData = await pollRes.json();

              // If job is done or failed, break loop
              if (jobData.status === 'completed' || jobData.status === 'failed') break;
            }
          } catch (pollErr) {
            console.error("Polling error", pollErr);
            // Allow retries
          }
        }

        if (jobData?.status === 'failed') {
          throw new Error(jobData.message || "Unknown error during analysis");
        }
        if (jobData?.result) {
          setReviewText(typeof jobData.result === 'string' ? jobData.result : JSON.stringify(jobData.result, null, 2));
          setActiveTab('review');
        } else {
          throw new Error("Analysis succeeded but returned no result");
        }

      } else {
        // Fallback for sync response (if any)
        setReviewText(typeof data.review === 'string' ? data.review : JSON.stringify(data.review, null, 2));
        setActiveTab('review');
      }

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

          {/* Project Selector */}
          <select
            value={selectedProject}
            onChange={(e) => setSelectedProject(e.target.value)}
            style={{
              background: '#0d1117',
              color: '#c9d1d9',
              border: '1px solid #30363d',
              padding: '4px 8px',
              borderRadius: '6px',
              fontSize: '0.9rem',
              marginLeft: '1rem'
            }}
          >
            <option value="TUTORIAL_RAG">Select Project...</option>
            {projects.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          <button onClick={logout} className="btn-action" style={{ background: 'rgba(255,255,255,0.1)', color: 'white' }}>
            <LogOut size={16} style={{ marginRight: 8 }} /> Logout
          </button>
        </div>
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
            <option value="sentinel">🛡️ Tier 0: Sentinel (Zero LLM)</option>
            <option value="tier1">🔍 Tier 1: Context Builder (Graph + Vector + LLM)</option>
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
        <button className={`tab-btn ${activeTab === 'graph' ? 'active' : ''}`} onClick={() => setActiveTab('graph')}>
          <GitPullRequest size={16} style={{ display: 'inline', marginRight: 6, verticalAlign: 'text-bottom' }} />
          Graph Viz
        </button>
        <button className={`tab-btn ${activeTab === 'vectors' ? 'active' : ''}`} onClick={() => setActiveTab('vectors')}>
          <span style={{ marginRight: 6 }}>🌌</span>
          Vector Space
        </button>
        <button className={`tab-btn ${activeTab === 'logs' ? 'active' : ''}`} onClick={() => setActiveTab('logs')}>
          <Terminal size={16} style={{ display: 'inline', marginRight: 6, verticalAlign: 'text-bottom' }} />
          System Logs
        </button>
      </div>

      {isLoading && (
        <div style={{ position: 'fixed', top: 0, left: 0, width: '100%', height: '100%', zIndex: 9999, background: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(5px)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '20px' }}>
          <div style={{ position: 'relative', width: '100%', maxWidth: '500px', height: '300px', borderRadius: '12px', overflow: 'hidden', boxShadow: '0 0 30px rgba(0, 255, 65, 0.2)', border: '1px solid #003b0f', background: '#000' }}>
            <MatrixRain />
          </div>
        </div>
      )}

      {/* Content */}
      <div className="tab-content" style={{ height: (activeTab === 'graph' || activeTab === 'vectors') ? 'calc(100vh - 200px)' : 'auto' }}>
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

        {activeTab === 'graph' && (
          <GraphView project={selectedProject} />
        )}

        {activeTab === 'vectors' && (
          <VectorView project={selectedProject} />
        )}

        {activeTab === 'logs' && (
          <div className="logs-container">
            <div className="logs-header">
              <span>Backend Event Stream</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <button onClick={handleCopyLogs} className="btn-action" style={{ padding: '4px 8px', fontSize: '0.8rem' }}>
                  <Copy size={14} style={{ marginRight: 4 }} /> Copy
                </button>
                <button onClick={fetchLogs} className="btn-action" style={{ padding: '4px 8px', fontSize: '0.8rem' }}>
                  <RefreshCcw size={14} style={{ marginRight: 4 }} /> Refresh
                </button>
                <span style={{ fontSize: '0.7rem', color: '#666' }}>MANUAL</span>
              </div>
            </div>
            <div className="terminal-body" id="term-body">
              {logs.length > 0 ? [...logs].reverse().map((l, i) => (
                <span key={i} className="log-line">
                  <span className="log-time">[{l.timestamp}]</span>
                  <span className={`log-lvl ${l.level}`}> {l.level}</span> {l.message}
                </span>
              )) : (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', opacity: 0.3 }}>
                  <Terminal size={48} style={{ marginBottom: 10 }} />
                  <p>System Initialized. No events yet.</p>
                  <p style={{ fontSize: '0.8rem' }}>Click Refresh to check for updates.</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Separate component for Graph to keep main component clean
function GraphView({ project }: { project: string }) {
  const [graphData, setGraphData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const url = project ? `/api/graph?project_name=${encodeURIComponent(project)}` : '/api/graph';
    setLoading(true);
    fetch(url)
      .then(res => res.json())
      .then(data => {
        // Transform for react-force-graph
        // Memgraph returns { nodes: [{id, labels, properties}], relationships: [{id, start, end, type}] }
        // ForceGraph needs { nodes: [{id}], links: [{source, target}] }

        const nodes = data.nodes.map((n: any) => ({
          id: n.node_id, // Matches CYPHER_EXPORT_NODES alias
          label: n.properties.name || 'Node',
          group: n.labels[0],
          ...n.properties
        }));

        const links = data.relationships.map((r: any) => ({
          source: r.from_id, // Matches CYPHER_EXPORT_RELATIONSHIPS alias
          target: r.to_id,   // Matches CYPHER_EXPORT_RELATIONSHIPS alias
          type: r.type
        }));

        setGraphData({ nodes, links });
        setLoading(false);
      })
      .catch(err => {
        console.error(err);
        setLoading(false);
      });
  }, [project]);

  if (loading) return <div style={{ padding: 20 }}>Loading Graph...</div>;
  if (!graphData) return <div style={{ padding: 20 }}>Failed to load graph</div>;

  return (
    <div style={{ width: '100%', height: '100%', background: '#0d1117', borderRadius: '8px', overflow: 'hidden' }}>
      <iframe
        srcDoc={`
                <html>
                    <head>
                        <script src="//unpkg.com/force-graph"></script>
                        <style>body { margin: 0; background-color: #0d1117; }</style>
                    </head>
                    <body>
                        <div id="graph"></div>
                        <script>
                            const data = ${JSON.stringify(graphData)};
                            const Graph = ForceGraph()(document.getElementById('graph'))
                                .graphData(data)
                                .nodeId('id')
                                .nodeLabel(n => \`[\${n.group}] \${n.label}\`)
                                .nodeAutoColorBy('group')
                                .nodeRelSize(6)
                                .linkWidth(link => link === window.highlightLink ? 4 : 1)
                                .linkDirectionalParticles(2)
                                .linkDirectionalParticleWidth(2)
                                .linkDirectionalArrowLength(3.5)
                                .linkDirectionalArrowRelPos(1)
                                .linkColor(() => 'rgba(255,255,255,0.2)')
                                .onNodeHover(node => {
                                    // Highlight neighbors
                                    if (node) {
                                      Graph.linkWidth(link => 
                                        link.source === node || link.target === node ? 3 : 0.5
                                      );
                                      Graph.linkColor(link => 
                                        link.source === node || link.target === node ? '#ff00ff' : 'rgba(255,255,255,0.05)'
                                      );
                                    } else {
                                      Graph.linkWidth(1);
                                      Graph.linkColor('rgba(255,255,255,0.2)');
                                    }
                                });
                        </script>
                    </body>
                </html>
            `}
        style={{ width: '100%', height: '100%', border: 'none' }}
      />
    </div>
  );
}

function VectorView({ project }: { project: string }) {
  const [graphData, setGraphData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const url = project ? `/api/vectors?project_name=${encodeURIComponent(project)}` : '/api/vectors';
    setLoading(true);
    fetch(url)
      .then(res => res.json())
      .then(data => {
        // Transform for 3d-force-graph
        // API returns { nodes: [{id, x, y, z, label, group, file}] }
        // We map x,y,z to fx,fy,fz to lock them in 3D space (Scatter Plot mode)
        // We scale them up a bit because PCA outputs are usually small (-1 to 1)
        const SCALE = 100;

        const nodes = data.nodes.map((n: any) => ({
          id: n.id,
          fx: n.x * SCALE,
          fy: n.y * SCALE,
          fz: n.z * SCALE,
          group: n.group,
          file: n.file,
          label: n.label
        }));

        setGraphData({ nodes, links: [] }); // No links in scatter plot
        setLoading(false);
      })
      .catch(err => {
        console.error(err);
        setLoading(false);
      });
  }, [project]);

  if (loading) return <div style={{ padding: 20 }}>Computing Vector Space (PCA)...</div>;
  if (!graphData) return <div style={{ padding: 20 }}>Failed to load vectors</div>;

  return (
    <div style={{ width: '100%', height: '100%', background: '#000', borderRadius: '8px', overflow: 'hidden' }}>
      <iframe
        srcDoc={`
                <html>
                    <head>
                        <script src="//unpkg.com/3d-force-graph"></script>
                        <style>body { margin: 0; background-color: #000; }</style>
                    </head>
                    <body>
                        <div id="3d-graph"></div>
                        <script>
                            const data = ${JSON.stringify(graphData)};
                            const Graph = ForceGraph3D()(document.getElementById('3d-graph'))
                                .graphData(data)
                                .nodeLabel(n => \`[\${n.group}] \${n.label} \n \${n.file}\`)
                                .nodeAutoColorBy('file') // Color by file to show clustering
                                .nodeVal(5)
                                .nodeOpacity(0.9)
                                .showNavInfo(false);
                                
                            // Disable forces to keep PCA positions
                            // Actually setting fx/fy/fz automatically overrides forces for those axes
                            // But we can turn off physics to save CPU
                            Graph.d3Force('charge', null);
                            Graph.d3Force('link', null);
                            Graph.d3Force('center', null);
                        </script>
                    </body>
                </html>
            `}
        style={{ width: '100%', height: '100%', border: 'none' }}
      />
    </div>
  );
}


function MatrixRain() {
  return (
    <div style={{
      position: 'relative',
      width: '100%',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(180deg, #0a0a0a 0%, #1a1a2e 100%)'
    }}>
      <div style={{ fontSize: '3rem', marginBottom: '2rem' }}>🔮</div>
      <div style={{ color: '#fff', fontFamily: 'monospace', fontSize: '1.2rem', marginBottom: '1.5rem', textAlign: 'center' }}>
        Analyzing PR...
      </div>
      <div style={{ width: '80%', maxWidth: '300px', height: '4px', background: 'rgba(255,255,255,0.1)', borderRadius: '2px', overflow: 'hidden' }}>
        <div style={{
          width: '100%',
          height: '100%',
          background: 'linear-gradient(90deg, #6366f1, #8b5cf6, #6366f1)',
          backgroundSize: '200% 100%',
          animation: 'shimmer 1.5s linear infinite',
          borderRadius: '2px'
        }} />
      </div>
      <div style={{ marginTop: '1rem', fontSize: '0.85rem', color: 'rgba(255,255,255,0.5)', fontFamily: 'monospace' }}>
        This may take a moment...
      </div>
      <style>{`
        @keyframes shimmer {
          0% { background-position: 200% 0; }
          100% { background-position: -200% 0; }
        }
      `}</style>
    </div>
  );
}
