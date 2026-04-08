#!/bin/bash

PYTHON_EXEC=$(which python)
LOG_FILE="$(pwd)/bot.log"

# Define absolute paths
CONTRACTS_DIR=$(realpath "../0g-restaking-contracts")

# Timing configuration
LAST_ASCEND_TIME=$(date +%s)
#LAST_ASCEND_TIME=0
ASCEND_INTERVAL=1209600  # 2 weeks in seconds
STEP2_INTERVAL=7200      # 2 hours in seconds (Step 2 execution interval)
LAST_STEP2_TIME=$(date +%s)
#LAST_STEP2_TIME=0
STEP3_INTERVAL=86400     # 1 day in seconds (Step 3 execution interval)
LAST_STEP3_TIME=$(date +%s)
#LAST_STEP3_TIME=0
DEFAULT_LOOP_SLEEP=300 # 5 minutes in seconds
POST_ASCEND_GAP=60     # 1 minute (wait time between Step 1 and Step 2)

# Epoch handling configuration
RPC_URL="https://evmrpc.0g.ai"
WITHDRAWAL_QUEUE="0x10A98a5344742308744Bd59829786584A12C1146"
PRIVATE_KEY=""

format_duration() {
    local secs=$1
    local d=$((secs / 86400))
    local h=$(( (secs % 86400) / 3600 ))
    local m=$(( (secs % 3600) / 60 ))
    local result=""
    (( d > 0 )) && result="${d}d "
    (( h > 0 )) && result="${result}${h}h "
    (( m > 0 )) && result="${result}${m}m"
    echo "${result:- <1m}"
}

echo "Starting operator_bot scheduler..." | tee -a "$LOG_FILE"
echo "Logging to: $LOG_FILE" | tee -a "$LOG_FILE"
echo "Schedule: Step 1 every 2 weeks, Step 2 every 2 hrs, Step 3 every 1 day, Step 4 every cycle." | tee -a "$LOG_FILE"

# Read immutable contract parameters once
echo "Reading WithdrawalQueue contract parameters..." | tee -a "$LOG_FILE"
INIT_TIMESTAMP=$(cast call "$WITHDRAWAL_QUEUE" "initTimestamp()(uint256)" --rpc-url "$RPC_URL" | awk '{print $1}')
EPOCH_DURATION=$(cast call "$WITHDRAWAL_QUEUE" "epochDuration()(uint256)" --rpc-url "$RPC_URL" | awk '{print $1}')
WITHDRAWAL_DELAY=$(cast call "$WITHDRAWAL_QUEUE" "withdrawalDelay()(uint256)" --rpc-url "$RPC_URL" | awk '{print $1}')
echo "  initTimestamp: $INIT_TIMESTAMP, epochDuration: $EPOCH_DURATION, withdrawalDelay: $WITHDRAWAL_DELAY" | tee -a "$LOG_FILE"

while true; do
    CURRENT_TIME=$(date +%s)
    # Default wait time for the next cycle
    NEXT_WAIT=$DEFAULT_LOOP_SLEEP

    echo -e "\n------------------------------------------" | tee -a "$LOG_FILE"
    echo "TIMESTAMP: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
    echo "------------------------------------------" | tee -a "$LOG_FILE"

    # --- 1. Execute ascend.sh (At interval timestamps) ---
    WAS_ASCEND_EXECUTED=false
    # Check if there's any interval timestamp between last execution and current time
    LAST_INTERVAL_MULTIPLE=$((LAST_ASCEND_TIME / ASCEND_INTERVAL * ASCEND_INTERVAL))
    NEXT_INTERVAL_MULTIPLE=$((CURRENT_TIME / ASCEND_INTERVAL * ASCEND_INTERVAL))
    if (( LAST_INTERVAL_MULTIPLE < NEXT_INTERVAL_MULTIPLE )); then
        if [ -d "$CONTRACTS_DIR" ]; then
            echo "[Task 1] Time threshold met. Running ascend.sh..." | tee -a "$LOG_FILE"
            pushd "$CONTRACTS_DIR" > /dev/null

            if [ -f "./ascend.sh" ]; then
                bash ./ascend.sh 2>&1 | tee -a "$LOG_FILE"
                # Update the last execution timestamp to the interval multiple
                LAST_ASCEND_TIME=$CURRENT_TIME
                WAS_ASCEND_EXECUTED=true
                echo "[Task 1] Complete. Timestamp updated to interval multiple." | tee -a "$LOG_FILE"
            else
                echo "ERROR: ascend.sh not found in $CONTRACTS_DIR" | tee -a "$LOG_FILE"
            fi
            popd > /dev/null
        else
            echo "ERROR: Contracts directory $CONTRACTS_DIR not found." | tee -a "$LOG_FILE"
        fi
    else
        NEXT_ASCEND_TIME=$((NEXT_INTERVAL_MULTIPLE + ASCEND_INTERVAL))
        ASCEND_REMAINING=$((NEXT_ASCEND_TIME - CURRENT_TIME))
        echo "[Task 1] Skipped. Next ascend in $(format_duration $ASCEND_REMAINING) (at $(date -d @$NEXT_ASCEND_TIME '+%Y-%m-%d %H:%M:%S'))." | tee -a "$LOG_FILE"
    fi

    # If Step 1 was executed, wait at least 1 minute before proceeding to Step 2-4
    if [ "$WAS_ASCEND_EXECUTED" = true ]; then
        echo "[Wait] Step 1 finished. Waiting ${POST_ASCEND_GAP}s for state synchronization..." | tee -a "$LOG_FILE"
        sleep $POST_ASCEND_GAP
    fi

    # --- 2. Run Python script (Step 2) ---
    # Check if there's any interval timestamp between last step2 execution and current time
    LAST_STEP2_INTERVAL_MULTIPLE=$((LAST_STEP2_TIME / STEP2_INTERVAL * STEP2_INTERVAL))
    NEXT_STEP2_INTERVAL_MULTIPLE=$((LAST_STEP2_INTERVAL_MULTIPLE + STEP2_INTERVAL))
    if (( NEXT_STEP2_INTERVAL_MULTIPLE <= CURRENT_TIME )); then
        echo "[Task 2] Running operator_bot.py..." | tee -a "$LOG_FILE"
        $PYTHON_EXEC -u ./src/web3_scripts/operator_bot.py -y 2>&1 | tee -a "$LOG_FILE"
        # Update the last step2 execution timestamp to the interval multiple
        LAST_STEP2_TIME=$NEXT_STEP2_INTERVAL_MULTIPLE
        echo "[Task 2] Complete." | tee -a "$LOG_FILE"
    else
        STEP2_REMAINING=$((NEXT_STEP2_INTERVAL_MULTIPLE - CURRENT_TIME))
        echo "[Task 2] Skipped. Next run in $(format_duration $STEP2_REMAINING) (at $(date -d @$NEXT_STEP2_INTERVAL_MULTIPLE '+%Y-%m-%d %H:%M:%S'))." | tee -a "$LOG_FILE"
    fi

    # --- 3. Run Python script (Step 3) ---
    # Check if there's any interval timestamp between last step3 execution and current time
    LAST_STEP3_INTERVAL_MULTIPLE=$((LAST_STEP3_TIME / STEP3_INTERVAL * STEP3_INTERVAL))
    NEXT_STEP3_INTERVAL_MULTIPLE=$((LAST_STEP3_INTERVAL_MULTIPLE + STEP3_INTERVAL))
    if (( NEXT_STEP3_INTERVAL_MULTIPLE <= CURRENT_TIME )); then
        echo "[Task 3] Running main.py..." | tee -a "$LOG_FILE"
        $PYTHON_EXEC -u ./src/main.py 2>&1 | tee -a "$LOG_FILE"
        # Update the last step3 execution timestamp to the interval multiple
        LAST_STEP3_TIME=$NEXT_STEP3_INTERVAL_MULTIPLE
        echo "[Task 3] Complete." | tee -a "$LOG_FILE"
    else
        STEP3_REMAINING=$((NEXT_STEP3_INTERVAL_MULTIPLE - CURRENT_TIME))
        echo "[Task 3] Skipped. Next run in $(format_duration $STEP3_REMAINING) (at $(date -d @$NEXT_STEP3_INTERVAL_MULTIPLE '+%Y-%m-%d %H:%M:%S'))." | tee -a "$LOG_FILE"
    fi

    # --- 4. Handle mature epochs on WithdrawalQueue ---
    echo "[Task 4] Checking for mature epochs..." | tee -a "$LOG_FILE"
    EPOCHS_PROCESSED=0
    while true; do
        EPOCH_ITERATOR=$(cast call "$WITHDRAWAL_QUEUE" "epochIterator()(uint256)" --rpc-url "$RPC_URL" | awk '{print $1}')
        CURRENT_EPOCH=$(cast call "$WITHDRAWAL_QUEUE" "currentEpoch()(uint256)" --rpc-url "$RPC_URL" | awk '{print $1}')

        if (( EPOCH_ITERATOR >= CURRENT_EPOCH )); then
            echo "[Task 4] Fully caught up (epochIterator=$EPOCH_ITERATOR, currentEpoch=$CURRENT_EPOCH)." | tee -a "$LOG_FILE"
            break
        fi

        BLOCK_TIMESTAMP=$(cast block latest --field timestamp --rpc-url "$RPC_URL" | awk '{print $1}')
        MATURITY_TIME=$(( INIT_TIMESTAMP + (EPOCH_ITERATOR + 1) * EPOCH_DURATION + WITHDRAWAL_DELAY ))

        if (( MATURITY_TIME > BLOCK_TIMESTAMP )); then
            echo "[Task 4] Epoch $EPOCH_ITERATOR not yet mature (maturity=$MATURITY_TIME, block=$BLOCK_TIMESTAMP)." | tee -a "$LOG_FILE"
            break
        fi

        echo "[Task 4] Processing epoch $EPOCH_ITERATOR..." | tee -a "$LOG_FILE"
        cast send "$WITHDRAWAL_QUEUE" "handleEpoch()" \
            --private-key "$PRIVATE_KEY" \
            --rpc-url "$RPC_URL" \
            --gas-price 3gwei \
            --priority-gas-price 3gwei 2>&1 | tee -a "$LOG_FILE"
        if [ ${PIPESTATUS[0]} -ne 0 ]; then
            echo "[Task 4] ERROR: Failed to process epoch $EPOCH_ITERATOR. Skipping." | tee -a "$LOG_FILE"
            break
        fi
        EPOCHS_PROCESSED=$(( EPOCHS_PROCESSED + 1 ))
        echo "[Task 4] Epoch $EPOCH_ITERATOR processed." | tee -a "$LOG_FILE"
    done
    if (( EPOCHS_PROCESSED > 0 )); then
        echo "[Task 4] Complete. Epochs processed: $EPOCHS_PROCESSED." | tee -a "$LOG_FILE"
    fi

    echo "Cycle complete. Sleeping for $(format_duration $NEXT_WAIT)..." | tee -a "$LOG_FILE"
    sleep $NEXT_WAIT
done