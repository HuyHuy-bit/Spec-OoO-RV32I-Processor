`default_nettype none
// blocking l1 cache, g0 geometry (config/cache.json): 1 kib, two ways, 32-byte lines, lru.
// sits between a core port and memory, both sides speaking memory_protocol_pkg.
// one transaction at a time, no hits under miss. semantics follow model/cache.py.
module l1_cache #(
  parameter bit WRITABLE = 1'b1
) (
  input  wire clk_i, rst_i,
  input  wire core_request_valid_i,
  output wire core_request_ready_o,
  input  memory_protocol_pkg::mem_request_t core_request_i,
  output wire core_response_valid_o,
  input  wire core_response_ready_i,
  output memory_protocol_pkg::mem_response_t core_response_o,
  output wire mem_request_valid_o,
  input  wire mem_request_ready_i,
  output memory_protocol_pkg::mem_request_t mem_request_o,
  input  wire mem_response_valid_i,
  output wire mem_response_ready_o,
  input  memory_protocol_pkg::mem_response_t mem_response_i,
  // d$: write back every dirty line, keep it valid. i$: invalidate every line.
  // hold valid until ready; ready is the one-cycle completion.
  input  wire flush_valid_i,
  output wire flush_ready_o,
  output wire busy_o,
  output wire fatal_o,
  output logic [1:0] fatal_reason_o
);
  import memory_protocol_pkg::*;

  localparam int OFFSET_BITS = 5, SETS = 16, INDEX_BITS = 4, TAG_BITS = 32 - OFFSET_BITS - INDEX_BITS;
  localparam logic [1:0] FATAL_WRITEBACK = 2'd1, FATAL_PROTOCOL = 2'd2;

  typedef enum logic [3:0] {
    IDLE, LOOKUP, WB_REQ, WB_RSP, FILL_REQ, FILL_RSP, PASS_REQ, PASS_RSP,
    RESPOND, FLUSH_SCAN, FLUSH_READ, FLUSH_DONE, FATAL
  } state_e;

  state_e state_q;
  mem_request_t req_q;
  mem_response_t rsp_q;
  // metadata lives in reset flops; leaf payload is never reset.
  logic [1:0] valid_q [SETS], dirty_q [SETS];
  logic lru_q [SETS];  // least recently used way
  logic victim_q, flushing_q;
  logic [INDEX_BITS+1:0] cursor_q;  // flush walk: {done, set, way}
  logic [31:0] wb_address_q;
  logic [255:0] wb_data_q;

  wire [INDEX_BITS-1:0] set = req_q.address[OFFSET_BITS +: INDEX_BITS];
  wire [TAG_BITS-1:0] tag = req_q.address[31 -: TAG_BITS];
  wire [INDEX_BITS-1:0] cursor_set = cursor_q[1 +: INDEX_BITS];
  wire cursor_way = cursor_q[0];

  // fatal: a response outside a waiting state, a foreign id, a protocol error on cache
  // traffic, or a payload field the transaction must leave zero.
  wire fill_ok = mem_response_i.status == MEM_STATUS_OK;
  wire mem_bad = !rst_i && state_q != FATAL && mem_response_valid_i && (!mem_response_ready_o
    || mem_response_i.transaction_id != req_q.transaction_id
    || (state_q != PASS_RSP && mem_response_i.status == MEM_STATUS_PROTOCOL_ERROR)
    || (state_q == WB_RSP && (mem_response_i.line_read_data != '0 || mem_response_i.uncached_read_data != '0))
    || (state_q == FILL_RSP && (mem_response_i.uncached_read_data != '0
                                || (!fill_ok && mem_response_i.line_read_data != '0))));

  wire core_take = core_request_valid_i && core_request_ready_o;
  wire core_done = core_response_valid_o && core_response_ready_i;
  wire mem_take = mem_request_valid_o && mem_request_ready_i;
  wire mem_done = mem_response_valid_i && mem_response_ready_o;
  // no handshake completes in reset, in fatal, or in the cycle a bad response is seen.
  wire active = !rst_i && state_q != FATAL && !mem_bad;
  wire flush_start = active && state_q == IDLE && flush_valid_i;

  assign core_request_ready_o = active && state_q == IDLE && !flush_valid_i;
  assign core_response_valid_o = active && state_q == RESPOND;
  assign core_response_o = core_response_valid_o ? rsp_q : '0;
  assign mem_request_valid_o = active && state_q inside {WB_REQ, FILL_REQ, PASS_REQ};
  assign mem_response_ready_o = !rst_i && state_q inside {WB_RSP, FILL_RSP, PASS_RSP};
  assign flush_ready_o = active && state_q == FLUSH_DONE;
  assign busy_o = state_q != IDLE;
  assign fatal_o = state_q == FATAL;

  // two ways, each a 23-bit tag leaf and two 128-bit masked data leaves.
  logic [1:0] tag_en, data_en;
  logic leaf_we;
  logic [INDEX_BITS-1:0] leaf_addr;
  logic [255:0] leaf_wdata;
  logic [31:0] leaf_wmask;
  logic [TAG_BITS-1:0] tag_rd [2];
  logic [255:0] data_rd [2];

  for (genvar w = 0; w < 2; w++) begin : g_way
    dense_memory_1rw #(.DEPTH(SETS), .WIDTH(TAG_BITS), .MASKED(1'b0)) tag_leaf (
      .clk_i, .en_i(tag_en[w]), .we_i(leaf_we), .addr_i(leaf_addr), .wdata_i(tag),
      .wmask_i(1'b1), .rdata_o(tag_rd[w]));
    for (genvar h = 0; h < 2; h++) begin : g_half
      dense_memory_1rw #(.DEPTH(SETS), .WIDTH(128), .MASKED(1'b1)) data_leaf (
        .clk_i, .en_i(data_en[w]), .we_i(leaf_we), .addr_i(leaf_addr),
        .wdata_i(leaf_wdata[h*128 +: 128]), .wmask_i(leaf_wmask[h*16 +: 16]),
        .rdata_o(data_rd[w][h*128 +: 128]));
    end
  end

  // lookup: leaves were read in the accept cycle and are valid now.
  wire [1:0] hit_way = {valid_q[set][1] && tag_rd[1] == tag, valid_q[set][0] && tag_rd[0] == tag};
  wire hit = |hit_way;
  wire hit_index = hit_way[1];
  wire victim = !valid_q[set][0] ? 1'b0 : !valid_q[set][1] ? 1'b1 : lru_q[set];
  wire illegal = !WRITABLE && (core_request_i.write || core_request_i.uncached);

  // a store miss merges its masked bytes over the fill.
  logic [255:0] store_bits, fill_merged;
  always_comb begin
    for (int b = 0; b < 32; b++) store_bits[b*8 +: 8] = {8{req_q.write && req_q.line_write_mask[b]}};
    fill_merged = (mem_response_i.line_read_data & ~store_bits) | (req_q.line_write_data & store_bits);
  end

  always_comb begin
    tag_en = '0;
    data_en = '0;
    leaf_we = 1'b0;
    leaf_addr = set;
    leaf_wdata = '0;
    leaf_wmask = '0;
    unique case (state_q)
      IDLE: if (core_take) begin
        tag_en = 2'b11;
        data_en = 2'b11;
        leaf_addr = core_request_i.address[OFFSET_BITS +: INDEX_BITS];
      end
      LOOKUP: if (hit && req_q.write) begin
        data_en = hit_way;
        leaf_we = 1'b1;
        leaf_wdata = req_q.line_write_data;
        leaf_wmask = req_q.line_write_mask;
      end
      FILL_RSP: if (mem_done && !mem_bad && fill_ok) begin
        tag_en[victim_q] = 1'b1;
        data_en[victim_q] = 1'b1;
        leaf_we = 1'b1;
        leaf_wdata = fill_merged;
        leaf_wmask = '1;
      end
      FLUSH_SCAN: if (!cursor_q[INDEX_BITS+1] && dirty_q[cursor_set][cursor_way]) begin
        tag_en[cursor_way] = 1'b1;
        data_en[cursor_way] = 1'b1;
        leaf_addr = cursor_set;
      end
      default: ;
    endcase
  end

  always_comb begin
    mem_request_o = '0;
    unique case (state_q)
      WB_REQ: begin
        mem_request_o.transaction_id = req_q.transaction_id;
        mem_request_o.write = 1'b1;
        mem_request_o.address = wb_address_q;
        mem_request_o.line_write_data = wb_data_q;
        mem_request_o.line_write_mask = '1;
      end
      FILL_REQ: begin
        mem_request_o.transaction_id = req_q.transaction_id;
        mem_request_o.address = {req_q.address[31:OFFSET_BITS], {OFFSET_BITS{1'b0}}};
      end
      PASS_REQ: mem_request_o = req_q;
      default: ;
    endcase
  end

  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= IDLE;
      fatal_reason_o <= '0;
      for (int s = 0; s < SETS; s++) begin
        valid_q[s] <= '0;
        dirty_q[s] <= '0;
        lru_q[s] <= 1'b0;
      end
    end else if (mem_bad) begin  // the first fatal reason sticks: mem_bad is low in FATAL
      state_q <= FATAL;
      fatal_reason_o <= FATAL_PROTOCOL;
    end else begin
      unique case (state_q)
        IDLE:
          if (flush_start) begin
            req_q <= '0;
            flushing_q <= 1'b1;
            cursor_q <= '0;
            if (WRITABLE) state_q <= FLUSH_SCAN;
            else begin
              for (int s = 0; s < SETS; s++) valid_q[s] <= '0;
              state_q <= FLUSH_DONE;
            end
          end else if (core_take) begin
            req_q <= core_request_i;
            flushing_q <= 1'b0;
            if (illegal) begin
              rsp_q <= '{transaction_id: core_request_i.transaction_id, status: MEM_STATUS_PROTOCOL_ERROR, default: '0};
              state_q <= RESPOND;
            end else state_q <= core_request_i.uncached ? PASS_REQ : LOOKUP;
          end
        LOOKUP:
          if (hit) begin
            lru_q[set] <= !hit_index;
            if (req_q.write) dirty_q[set][hit_index] <= 1'b1;
            rsp_q <= '{transaction_id: req_q.transaction_id, status: MEM_STATUS_OK,
                       line_read_data: req_q.write ? '0 : data_rd[hit_index], default: '0};
            state_q <= RESPOND;
          end else begin
            // the victim leaves the cache before its writeback or the fill.
            victim_q <= victim;
            valid_q[set][victim] <= 1'b0;
            dirty_q[set][victim] <= 1'b0;
            wb_address_q <= {tag_rd[victim], set, {OFFSET_BITS{1'b0}}};
            wb_data_q <= data_rd[victim];
            state_q <= valid_q[set][victim] && dirty_q[set][victim] ? WB_REQ : FILL_REQ;
          end
        WB_REQ: if (mem_take) state_q <= WB_RSP;
        WB_RSP:
          if (mem_done) begin
            if (mem_response_i.status != MEM_STATUS_OK) begin
              state_q <= FATAL;
              fatal_reason_o <= FATAL_WRITEBACK;
            end else state_q <= flushing_q ? FLUSH_SCAN : FILL_REQ;
          end
        FILL_REQ: if (mem_take) state_q <= FILL_RSP;
        FILL_RSP:
          if (mem_done) begin
            // a failed fill installs nothing; the victim is already gone.
            if (fill_ok) begin
              valid_q[set][victim_q] <= 1'b1;
              dirty_q[set][victim_q] <= req_q.write;
              lru_q[set] <= !victim_q;
            end
            rsp_q <= '{transaction_id: req_q.transaction_id, status: mem_response_i.status,
                       line_read_data: req_q.write ? '0 : mem_response_i.line_read_data, default: '0};
            state_q <= RESPOND;
          end
        PASS_REQ: if (mem_take) state_q <= PASS_RSP;
        PASS_RSP:
          if (mem_done) begin
            rsp_q <= mem_response_i;
            state_q <= RESPOND;
          end
        RESPOND: if (core_done) state_q <= IDLE;
        FLUSH_SCAN:
          if (cursor_q[INDEX_BITS+1]) state_q <= FLUSH_DONE;
          else if (dirty_q[cursor_set][cursor_way]) state_q <= FLUSH_READ;
          else cursor_q <= cursor_q + 1'b1;
        FLUSH_READ: begin
          dirty_q[cursor_set][cursor_way] <= 1'b0;
          wb_address_q <= {tag_rd[cursor_way], cursor_set, {OFFSET_BITS{1'b0}}};
          wb_data_q <= data_rd[cursor_way];
          cursor_q <= cursor_q + 1'b1;
          state_q <= WB_REQ;
        end
        FLUSH_DONE: if (flush_valid_i) state_q <= IDLE;
        FATAL: ;
        default: state_q <= FATAL;
      endcase
    end
  end

`ifndef SYNTHESIS
  always_ff @(posedge clk_i) begin
    if (!rst_i && core_take)
      assert (core_request_i.uncached || core_request_i.address[OFFSET_BITS-1:0] == '0)
        else $fatal(1, "L1_CACHE_UNALIGNED_LINE");
  end
`endif
endmodule
`default_nettype wire
