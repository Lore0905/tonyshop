require("dotenv").config();
const axios = require("axios");

const SHOPIFY_SHOP_DOMAIN = process.env.SHOPIFY_SHOP_DOMAIN;
const SHOPIFY_CLIENT_ID = process.env.SHOPIFY_CLIENT_ID;
const SHOPIFY_CLIENT_SECRET = process.env.SHOPIFY_CLIENT_SECRET;

async function validateShopifyToken() {
  console.log("Shop domain:", SHOPIFY_SHOP_DOMAIN);
  console.log("Client ID:", SHOPIFY_CLIENT_ID ? "presente" : "MANCANTE");
  console.log("Client Secret:", SHOPIFY_CLIENT_SECRET ? "presente" : "MANCANTE");

  // 1. Ottieni il token
  const tokenUrl = `https://${SHOPIFY_SHOP_DOMAIN}/admin/oauth/access_token`;

  const tokenResponse = await axios.post(
    tokenUrl,
    new URLSearchParams({
      grant_type: "client_credentials",
      client_id: SHOPIFY_CLIENT_ID,
      client_secret: SHOPIFY_CLIENT_SECRET,
    }).toString(),
    {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    }
  );

  const accessToken = tokenResponse.data.access_token;
  console.log("✅ Token ottenuto:", accessToken);

  // 2. Valida il token con una query GraphQL semplice
  const testResponse = await axios.post(
    `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/2024-01/graphql.json`,
    { query: "{ shop { name id } }" },
    {
      headers: {
        "X-Shopify-Access-Token": accessToken,
        "Content-Type": "application/json",
      },
    }
  );

  console.log("✅ Token valido! Shop:", testResponse.data.data.shop.name);
  return accessToken;
}

validateShopifyToken().catch((err) => {
  console.error("❌ ERRORE:");
  if (err.response) {
    console.error("Status:", err.response.status);
    console.error("Data:", JSON.stringify(err.response.data, null, 2));
  } else {
    console.error(err.message);
  }
  process.exit(1);
});