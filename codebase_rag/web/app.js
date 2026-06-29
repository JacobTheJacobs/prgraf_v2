document.addEventListener("DOMContentLoaded", () => {
    const prInput = document.getElementById("pr-url");
    const analyzeBtn = document.getElementById("analyze-btn");
    const output = document.getElementById("review-output");
    const logArea = document.getElementById("log-area");

    analyzeBtn.addEventListener("click", runReview);
    document.getElementById("refresh-logs-btn").addEventListener("click", fetchLogs);

    async function runReview() {
        let parsed;
        try {
            parsed = parsePrUrl(prInput.value.trim());
        } catch (error) {
            setOutput(error.message);
            return;
        }

        analyzeBtn.disabled = true;
        setOutput("Checking changed files...");

        try {
            const response = await fetch("/api/analyze", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(parsed),
            });
            const data = await response.json();
            if (!response.ok || data.status === "error") {
                throw new Error(data.message || `HTTP ${response.status}`);
            }
            setOutput(data.review || "Pre-Landing Review: no result returned.");
        } catch (error) {
            setOutput(`Review failed: ${error.message}`);
        } finally {
            analyzeBtn.disabled = false;
            fetchLogs();
        }
    }

    function parsePrUrl(url) {
        const match = url.match(/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
        if (!match) {
            throw new Error("Invalid PR URL. Use https://github.com/org/repo/pull/123");
        }
        return {
            repo_url: `https://github.com/${match[1]}/${match[2]}.git`,
            pr_number: Number(match[3]),
        };
    }

    async function fetchLogs() {
        try {
            const response = await fetch("/api/logs");
            const data = await response.json();
            logArea.textContent = (data.logs || [])
                .slice()
                .reverse()
                .map((log) => `[${log.timestamp}] ${log.level} ${log.message}`)
                .join("\n");
        } catch (error) {
            logArea.textContent = `Failed to load logs: ${error.message}`;
        }
    }

    function setOutput(text) {
        output.textContent = text;
    }
});
