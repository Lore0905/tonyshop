require("dotenv").config();
const axios = require("axios");

const SHOPIFY_SHOP_DOMAIN = process.env.SHOPIFY_SHOP_DOMAIN;

const { validateShopifyToken } = require("./commons")


async function getLocations() {

    const accessToken = await validateShopifyToken();
    
  const response = await axios.post(
    `https://${SHOPIFY_SHOP_DOMAIN}/admin/api/2025-07/graphql.json`,
    {
      query: `
        {
          locations(first: 100) {
            edges {
              node {
                id
                name
                isActive
                fulfillsOnlineOrders
              }
            }
          }
        }
      `,
    },
    {
      headers: {
        "X-Shopify-Access-Token": accessToken,
        "Content-Type": "application/json",
      },
    }
  );

  const locations = response.data.data.locations.edges;

  console.log(`📍 Trovate ${locations.length} location:\n`);

  locations.forEach(({ node }) => {
    console.log({
      id: node.id,
      name: node.name,
      active: node.isActive,
      fulfillsOnlineOrders: node.fulfillsOnlineOrders,
    });
  });
}

getLocations().catch((err) => {
  console.error("❌ Errore:", err.message);
  if (err.response) {
    console.error("Status:", err.response.status);
    console.error("Data:", JSON.stringify(err.response.data, null, 2));
  }
});