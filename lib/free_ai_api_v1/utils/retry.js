async function withRetry(fn, { retries = 1, shouldRetry = () => true, onRetry = () => {} } = {}) {
    let lastError;
    for (let i = 0; i <= retries; i++) {
        try {
            return await fn();
        } catch (error) {
            lastError = error;
            if (i < retries && shouldRetry(error)) {
                onRetry(error, i + 1);
            } else {
                throw error;
            }
        }
    }
    throw lastError;
}

module.exports = { withRetry };
