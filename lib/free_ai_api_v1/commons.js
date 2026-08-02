function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function maskString(str, start = 4, end = 4) {
    if (!str || str.length <= start + end) return "****";
    return `${str.substring(0, start)}...${str.substring(str.length - end)}`;
}

module.exports = {
    sleep,
    maskString
};
