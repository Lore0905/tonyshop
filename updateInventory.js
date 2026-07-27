require("dotenv").config();
const fs = require("fs");


const {
    getVendorProducts,
    getShopifyProducts,
    updateShopifyInventory
} = require("./commons");


async function updateInventory() {
    console.log("🚀 Start inventory sync");

    const sourceProducts = await getVendorProducts();

    const shopifyProducts = await getShopifyProducts();

    let inventoryDataToUpdate = [];

    for (const product of sourceProducts) {

        const sku = product["Riferimento"];
        const quantity = Number(product["Quantità"]);

        if (!sku) continue;

        const shopifyProduct = shopifyProducts[sku];

        if (!shopifyProduct) {
            console.log(`⚠️ SKU non trovato Shopify: ${sku}`);
            continue;
        }

        if (shopifyProduct.quantity !== quantity && quantity) {

            console.log(`Aggiorno ${sku}: ${shopifyProduct.quantity} -> ${quantity}`);
            inventoryDataToUpdate.push({
                inventoryItemId: shopifyProduct.inventoryItemId,
                quantity
            })
        }

    }

    if (inventoryDataToUpdate.length > 0) {
        await updateShopifyInventory(inventoryDataToUpdate);
    }
    console.log("✅ Sync completato");
}


updateInventory()
.catch(error => {
    console.error(error);
    fs.writeFileSync('./log/log' + new Date().getTime() + '.json', JSON.stringify(error));
    
    process.exit(1);

});