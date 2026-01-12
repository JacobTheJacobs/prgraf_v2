import { NextRequest, NextResponse } from 'next/server';

export async function POST(request: NextRequest) {
    try {
        const body = await request.json();

        const response = await fetch('http://localhost:8000/api/analyze', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
        });

        const data = await response.json();
        return NextResponse.json(data, { status: response.status });
    } catch (error: any) {
        console.error('Proxy error:', error);
        return NextResponse.json(
            { status: 'error', message: error.message || 'Failed to connect to backend' },
            { status: 500 }
        );
    }
}
