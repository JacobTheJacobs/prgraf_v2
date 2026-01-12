"use client";

import React, { createContext, useContext, useEffect, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';

interface AuthContextType {
    token: string | null;
    login: (token: string) => void;
    logout: () => void;
    isLoading: boolean;
}

const AuthContext = createContext<AuthContextType>({
    token: null,
    login: () => { },
    logout: () => { },
    isLoading: true,
});

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
    const [token, setToken] = useState<string | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const router = useRouter();
    const pathname = usePathname();

    useEffect(() => {
        const stored = localStorage.getItem('gh_token');
        if (stored) {
            setToken(stored);
        }
        setIsLoading(false);
    }, []);

    useEffect(() => {
        if (!isLoading) {
            const isLoginPage = pathname === '/login';
            if (!token && !isLoginPage) {
                router.push('/login');
            } else if (token && isLoginPage) {
                router.push('/');
            }
        }
    }, [token, isLoading, pathname, router]);

    const login = (newToken: string) => {
        localStorage.setItem('gh_token', newToken);
        setToken(newToken);
        router.push('/');
    };

    const logout = () => {
        localStorage.removeItem('gh_token');
        setToken(null);
        router.push('/login');
    };

    return (
        <AuthContext.Provider value={{ token, login, logout, isLoading }}>
            {!isLoading ? children : <div style={{ display: 'flex', justifyContent: 'center', marginTop: '20vh' }}>Loading...</div>}
        </AuthContext.Provider>
    );
};

export const useAuth = () => useContext(AuthContext);
