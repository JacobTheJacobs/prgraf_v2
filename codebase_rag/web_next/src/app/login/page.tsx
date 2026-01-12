"use client";
import React, { useState } from 'react';
import { useAuth } from '@/context/AuthContext';
import { Github, Key } from 'lucide-react';

export default function LoginPage() {
    const { login } = useAuth();
    const [tokenInput, setTokenInput] = useState('');
    const [error, setError] = useState('');

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (tokenInput.trim().startsWith('gh') || tokenInput.trim().length > 10) {
            // Relaxed check for PATs that might be different format but usually start with gh
            // We accept it if it looks roughly right
            login(tokenInput.trim());
        } else {
            setError("Token seems invalid. Should be a GitHub PAT.");
        }
    };

    return (
        <div className="glass-card">
            <div className="brand-header">
                <span className="logo-icon">🔮</span>
                <h1>PocketFlow PR Reviewer</h1>
                <p>AI-Powered Codebase Intelligence</p>
            </div>

            <form onSubmit={handleSubmit}>
                <div className="input-group">
                    <label>GitHub Access Token</label>
                    <div style={{ position: 'relative' }}>
                        <Key size={16} style={{ position: 'absolute', left: '12px', top: '12px', zIndex: 1, color: '#666' }} />
                        <input
                            type="password"
                            placeholder="ghp_..."
                            value={tokenInput}
                            onChange={(e) => {
                                setTokenInput(e.target.value);
                                setError('');
                            }}
                            style={{ paddingLeft: '36px' }}
                        />
                    </div>
                    {error && <span style={{ color: '#f87171', fontSize: '0.8rem', marginTop: '0.5rem', display: 'block' }}>{error}</span>}
                    <span className="hint">Required for fetching PR diffs and repository access.</span>
                </div>

                <button type="submit" className="btn-primary">
                    Initialize Session
                </button>

                <div style={{ marginTop: '1.5rem' }}>
                    <a
                        href="https://github.com/settings/tokens"
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '0.5rem',
                            color: 'var(--text-secondary)',
                            fontSize: '0.8rem',
                            textDecoration: 'none'
                        }}
                    >
                        <Github size={14} />
                        Generate Token
                    </a>
                </div>
            </form>
        </div>
    );
}
